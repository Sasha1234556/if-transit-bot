import web
import storage
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
import pytz
import requests

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import (
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

BOT_TOKEN = "6241822896:AAHs62SrJ6RgLv58QXqvf32AQWSNRUyWXlw"
ADMIN_ID = 1589200303  # ваш Telegram ID

KYIV_TZ = pytz.timezone("Europe/Kyiv")
SCHEDULES_FILE = "schedules.json"
SUBS_FILE = "subscriptions.json"

MENU_BUTTONS = ["🚎 Маршрути", "🔔 Мої зупинки", "🗺 Онлайн-карта", "💬 Відгуки"]

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


class RemindState(StatesGroup):
    waiting_minutes = State()


class FeedbackState(StatesGroup):
    waiting_feedback = State()
    waiting_reply = State()


MIN_REMIND = 1
MAX_REMIND = 60

# Словник для відстеження стану тривоги для користувачів
user_alarm_status = {}
# Час останнього реального відбою тривоги (для попередження про збитий графік протягом 1 години)
last_all_clear_time = None
# Ручне перевизначення тривоги адміністратором: None = авто (API), True = примусова тривога
manual_alarm_override = None
# Останній відомий стан тривоги за автоперевіркою/ручним режимом
last_effective_alarm_state = False
# Чи була поточна/остання тривога реальною (зафіксованою через API), а не тренувальною через /alarm_on
was_real_alarm = False


def load_json(filename, default):
    if os.path.exists(filename):
        with open(filename, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(filename, data):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


schedules = load_json(SCHEDULES_FILE, {})
subscriptions = storage.load_subscriptions()
users_data = storage.load_users()


def _format_user_label(user_id: str, info: dict) -> str:
    username = info.get("username")
    full_name = info.get("full_name")
    if username:
        return f"@{username} (`{user_id}`)"
    if full_name:
        return f"{full_name} (`{user_id}`)"
    return f"`{user_id}`"


async def record_user_activity(user: types.User):
    """Записує активність користувача та повідомляє лише адміна про нового користувача."""
    if not user or user.is_bot:
        return

    user_id = str(user.id)
    now_str = datetime.now(KYIV_TZ).strftime("%Y-%m-%d %H:%M")
    is_new = user_id not in users_data
    was_inactive = (not is_new) and (not users_data[user_id].get("active", True))

    entry = users_data.get(user_id, {})
    if is_new:
        entry["first_seen"] = now_str
    entry["username"] = user.username
    entry["full_name"] = user.full_name
    entry["last_seen"] = now_str
    entry["active"] = True
    users_data[user_id] = entry
    storage.save_users(users_data)

    if ADMIN_ID and int(user.id) != ADMIN_ID:
        label = _format_user_label(user_id, entry)
        if is_new:
            try:
                await bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(
                        f"🆕 **Новий користувач у боті!**\n"
                        f"👤 {label}\n"
                        f"🕒 Час: {now_str}\n"
                        f"👥 Всього користувачів: **{len(users_data)}**"
                    ),
                    parse_mode="Markdown",
                )
            except Exception as e:
                logging.error(f"Не вдалося надіслати сповіщення про нового користувача {user_id}: {e}")
        elif was_inactive:
            try:
                await bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(
                        f"♻️ **Користувач повернувся до бота!**\n"
                        f"👤 {label}\n"
                        f"🕒 Час: {now_str}"
                    ),
                    parse_mode="Markdown",
                )
            except Exception as e:
                logging.error(f"Не вдалося надіслати сповіщення про повернення користувача {user_id}: {e}")


@dp.my_chat_member()
async def handle_my_chat_member(event: ChatMemberUpdated):
    """Відстежує блокування/зупинку або розблокування бота користувачем (сповіщення лише адміну)."""
    user = event.from_user
    if not user or user.is_bot:
        return

    user_id = str(user.id)
    now_str = datetime.now(KYIV_TZ).strftime("%Y-%m-%d %H:%M")
    new_status = event.new_chat_member.status

    entry = users_data.setdefault(user_id, {"first_seen": now_str})
    entry["username"] = user.username
    entry["full_name"] = user.full_name
    entry["last_seen"] = now_str

    if new_status in ("kicked", "left"):
        entry["active"] = False
        entry["left_at"] = now_str
        storage.save_users(users_data)
        if ADMIN_ID and int(user.id) != ADMIN_ID:
            label = _format_user_label(user_id, entry)
            try:
                await bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(
                        f"🚪 **Користувач зупинив/заблокував бота**\n"
                        f"👤 {label}\n"
                        f"🕒 Час: {now_str}"
                    ),
                    parse_mode="Markdown",
                )
            except Exception as e:
                logging.error(f"Не вдалося надіслати сповіщення про вихід користувача {user_id}: {e}")
    elif new_status == "member":
        entry["active"] = True
        storage.save_users(users_data)


@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    """Статистика користувачів лише для адміністратора."""
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return

    await record_user_activity(message.from_user)

    all_ids = set(users_data.keys()) | set(subscriptions.keys())
    total_users = len(all_ids)
    active_users = sum(1 for uid in all_ids if users_data.get(uid, {}).get("active", True))
    left_users = total_users - active_users
    users_with_subs = sum(1 for uid, subs in subscriptions.items() if subs)

    sorted_users = sorted(
        users_data.items(),
        key=lambda item: item[1].get("last_seen", ""),
        reverse=True,
    )[:10]

    recent_lines = []
    for uid, info in sorted_users:
        status_icon = "🟢" if info.get("active", True) else "🔴"
        label = _format_user_label(uid, info)
        seen = info.get("last_seen", "—")
        recent_lines.append(f"{status_icon} {label} — {seen}")

    recent_block = "\n".join(recent_lines) if recent_lines else "Поки немає даних."

    await message.answer(
        f"📊 **Статистика бота**\n"
        f"👥 Всього користувачів: **{total_users}**\n"
        f"🟢 Активних: **{active_users}**\n"
        f"🔴 Заблокували/зупинили: **{left_users}**\n"
        f"🔔 Зі збереженими зупинками: **{users_with_subs}**\n\n"
        f"🕒 **Останні активні користувачі:**\n{recent_block}",
        parse_mode="Markdown",
    )


@dp.message(Command("alarm_on"))
async def cmd_alarm_on(message: types.Message, state: FSMContext):
    """Примусово вмикає тренувальний/ручний режим тривоги та паузу сповіщень (лише для адміна)."""
    global manual_alarm_override, was_real_alarm
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    manual_alarm_override = True
    was_real_alarm = False
    await message.answer(
        "🔴 **Увімкнено ручний режим тривоги (тренувальний).**\n"
        "Бот сповістить користувачів про червоний рівень небезпеки та призупинить нагадування про вихід на зупинку.\n"
        "Після `/alarm_off` попередження про відхилення від графіка на 1 годину **не вмикатиметься**.",
        parse_mode="Markdown",
    )
    await check_schedules()


@dp.message(Command("alarm_off"))
async def cmd_alarm_off(message: types.Message, state: FSMContext):
    """Знімає ручну тривогу та повертає бота в автоматичний режим перевірки API (лише для адміна)."""
    global manual_alarm_override, last_all_clear_time, was_real_alarm
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    had_manual_alarm = manual_alarm_override is True
    manual_alarm_override = None

    if was_real_alarm and not had_manual_alarm:
        last_all_clear_time = datetime.now(KYIV_TZ)
        was_real_alarm = False
        await message.answer(
            "🟢 **Реальну тривогу завершено, відновлено автоперевірку API.**\n"
            "Протягом 1 години у нагадуваннях буде попередження про можливе відхилення від графіка.",
            parse_mode="Markdown",
        )
    else:
        was_real_alarm = False
        last_all_clear_time = None
        await message.answer(
            "🟢 **Ручний режим вимкнено, відновлено автоперевірку API.**\n"
            "Оскільки це була тренувальна перевірка, попередження про відхилення від графіка не вмикається.",
            parse_mode="Markdown",
        )
    await check_schedules()


@dp.message(Command("alarm_status"))
async def cmd_alarm_status(message: types.Message, state: FSMContext):
    """Показує поточний статус тривоги та вікна стабілізації (лише для адміна)."""
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    now = datetime.now(KYIV_TZ)
    api_alarm = check_ivano_frankivsk_alarm()
    effective_alarm = get_effective_alarm_status()
    post_alarm_active = (
        last_all_clear_time is not None
        and (now - last_all_clear_time) < timedelta(hours=1)
    )
    mode_label = (
        "🔴 Ручний / тренувальний (/alarm_on)"
        if manual_alarm_override is True
        else "🤖 Автоматично через API"
    )
    clear_str = last_all_clear_time.strftime("%H:%M") if last_all_clear_time else "—"
    await message.answer(
        f"🚨 **Статус тривоги:** {'🔴 АКТИВНА' if effective_alarm else '🟢 НЕМАЄ'}\n"
        f"⚙️ **Режим:** {mode_label}\n"
        f"🌐 **Дані API:** {'тривога' if api_alarm else 'спокійно'}\n"
        f"🕒 **Останній відбій (реальний):** {clear_str}\n"
        f"⏳ **Попередження про відхилення (1 год після реального відбою):** {'так' if post_alarm_active else 'ні'}",
        parse_mode="Markdown",
    )


# Клавіатура з посиланнями на живі карти Івано-Франківська та мапу тривог
map_keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="🗺 DozoR (Карта)", url="https://city.dozor.tech/ua/iv-frankivsk/city"),
        InlineKeyboardButton(text="🚍 EasyWay", url="https://www.eway.in.ua/ua/cities/frankivsk")
    ],
    [
        InlineKeyboardButton(text="🚨 Мапа тривог", url="https://map.ukrainealarm.com/")
    ]
])


def main_menu_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🚎 Маршрути"), KeyboardButton(text="🔔 Мої зупинки")],
            [KeyboardButton(text="🗺 Онлайн-карта"), KeyboardButton(text="💬 Відгуки")]
        ],
        resize_keyboard=True,
    )


def cancel_feedback_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Скасувати", callback_data="feedback:cancel")]
    ])


async def send_admin_feedback_history(target_message: types.Message):
    """Показує адміністратору історію відгуків і кнопки швидкої відповіді користувачам."""
    feedbacks = storage.load_feedbacks()
    if not feedbacks:
        await target_message.answer("📭 Поки що немає жодного збереженого відгуку.")
        return

    indexed_recent = list(enumerate(feedbacks))[-10:]
    indexed_recent.reverse()

    lines = [f"💬 **Історія відгуків** (всього: **{len(feedbacks)}**, показані останні {len(indexed_recent)}):\n"]
    buttons = []

    for real_idx, item in indexed_recent:
        uid = item.get("user_id")
        username = item.get("username")
        full_name = item.get("full_name")
        created_at = item.get("created_at", "—")
        text = item.get("text", "")

        if username:
            who = f"@{username} (`{uid}`)"
        elif full_name:
            who = f"{full_name} (`{uid}`)"
        else:
            who = f"`{uid}`"

        lines.append(
            f"**#{real_idx + 1}** | 🕒 {created_at}\n"
            f"👤 Від: {who}\n"
            f"📝 {text}\n"
        )

        if uid:
            short_name = f"@{username}" if username else (full_name or str(uid))
            short_name = short_name[:18]
            buttons.append([
                InlineKeyboardButton(
                    text=f"↩️ Відповісти #{real_idx + 1} ({short_name})",
                    callback_data=f"fb_reply:{real_idx}",
                )
            ])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    await target_message.answer("\n".join(lines), reply_markup=kb, parse_mode="Markdown")


@dp.message(Command("feedbacks"))
async def cmd_feedbacks(message: types.Message, state: FSMContext):
    """Команда перегляду історії відгуків лише для адміністратора."""
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await record_user_activity(message.from_user)
    await send_admin_feedback_history(message)


@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await record_user_activity(message.from_user)
    await message.answer(
        "Привіт! Я бот розкладу тролейбусів та автобусів Івано-Франківська.\n"
        "Натисни **«🚎 Маршрути»**, скористайся кнопкою карти внизу або просто напиши назву своєї зупинки текстом, і я знайду її розклад!",
        reply_markup=main_menu_kb(),
        parse_mode="Markdown",
    )


@dp.message(F.text == "🗺 Онлайн-карта")
async def open_map_links(message: types.Message, state: FSMContext):
    await state.clear()
    await record_user_activity(message.from_user)
    await message.answer(
        "Оберіть сервіс для перегляду транспорту наживо або мапу тривог:",
        reply_markup=map_keyboard
    )


# --- Відгуки користувачів та історія/відповіді для адміна ---
@dp.message(F.text == "💬 Відгуки")
async def start_feedback(message: types.Message, state: FSMContext):
    await record_user_activity(message.from_user)
    if ADMIN_ID and message.from_user.id == ADMIN_ID:
        await state.clear()
        await send_admin_feedback_history(message)
        return

    await state.set_state(FeedbackState.waiting_feedback)
    await message.answer(
        "💬 **Напишіть ваш відгук або пропозицію**\n"
        "Поділіться враженнями від роботи бота, зауваженнями щодо розкладів чи ідеями для покращення. "
        "Надішліть текст одним повідомленням:",
        reply_markup=cancel_feedback_kb(),
        parse_mode="Markdown",
    )


@dp.callback_query(F.data.startswith("fb_reply:"))
async def start_admin_reply(callback: types.CallbackQuery, state: FSMContext):
    if not ADMIN_ID or callback.from_user.id != ADMIN_ID:
        await callback.answer("Недостатньо прав", show_alert=True)
        return

    try:
        idx = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Некоректний запит", show_alert=True)
        return

    feedbacks = storage.load_feedbacks()
    if idx < 0 or idx >= len(feedbacks):
        await callback.answer("Відгук не знайдено", show_alert=True)
        return

    item = feedbacks[idx]
    target_user_id = item.get("user_id")
    if not target_user_id:
        await callback.answer("У відгуку відсутній ID користувача", show_alert=True)
        return

    username = item.get("username")
    full_name = item.get("full_name")
    who = f"@{username} (`{target_user_id}`)" if username else f"{full_name or target_user_id} (`{target_user_id}`)"

    await state.set_state(FeedbackState.waiting_reply)
    await state.update_data(
        reply_to_user_id=int(target_user_id),
        reply_to_label=who,
        original_feedback=item.get("text", ""),
    )

    await callback.answer()
    await callback.message.answer(
        f"✍️ **Відповідь на відгук #{idx + 1}**\n"
        f"👤 Кому: {who}\n"
        f"💬 Відгук: _{item.get('text', '')}_\n\n"
        f"Напишіть текст відповіді одним повідомленням:",
        reply_markup=cancel_feedback_kb(),
        parse_mode="Markdown",
    )


@dp.message(FeedbackState.waiting_reply, F.text & ~F.text.startswith("/"))
async def send_admin_reply(message: types.Message, state: FSMContext):
    if message.text in MENU_BUTTONS:
        await state.clear()
        raise SkipHandler

    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        await state.clear()
        return

    reply_text = message.text.strip()
    if len(reply_text) < 1:
        await message.answer("⚠️ Введіть текст відповіді або натисніть «Скасувати».")
        return

    data = await state.get_data()
    target_user_id = data.get("reply_to_user_id")
    who = data.get("reply_to_label", f"`{target_user_id}`")
    await state.clear()

    if not target_user_id:
        await message.answer("❌ Не вдалося визначити отримувача.")
        return

    try:
        await bot.send_message(
            chat_id=int(target_user_id),
            text=(
                f"📩 **Відповідь від адміністратора бота:**\n\n"
                f"{reply_text}"
            ),
            parse_mode="Markdown",
        )
        await message.answer(
            f"✅ Відповідь надіслано користувачу {who}.",
            reply_markup=main_menu_kb(),
            parse_mode="Markdown",
        )
    except Exception as e:
        logging.error(f"Не вдалося надіслати відповідь користувачу {target_user_id}: {e}")
        await message.answer(
            f"❌ Не вдалося доставити повідомлення користувачу {who} (можливо, він заблокував бота).",
            reply_markup=main_menu_kb(),
            parse_mode="Markdown",
        )


@dp.callback_query(F.data == "feedback:cancel")
async def cancel_feedback(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer("Скасовано")
    try:
        await callback.message.edit_text("Дію скасовано.")
    except TelegramBadRequest:
        pass


@dp.message(FeedbackState.waiting_feedback, F.text & ~F.text.startswith("/"))
async def receive_feedback(message: types.Message, state: FSMContext):
    # Якщо користувач натиснув іншу кнопку головного меню — скидаємо стан і передаємо далі
    if message.text in MENU_BUTTONS:
        await state.clear()
        raise SkipHandler

    await record_user_activity(message.from_user)
    feedback_text = message.text.strip()
    if len(feedback_text) < 2:
        await message.answer("⚠️ Відгук занадто короткий. Напишіть, будь ласка, кілька слів або натисніть «Скасувати».")
        return

    now_str = datetime.now(KYIV_TZ).strftime("%Y-%m-%d %H:%M")
    user = message.from_user
    entry = {
        "user_id": user.id,
        "username": user.username,
        "full_name": user.full_name,
        "text": feedback_text,
        "created_at": now_str,
    }
    storage.save_feedback(entry)
    await state.clear()

    await message.answer(
        "🙏 **Дякуємо за ваш відгук!** Ми обов'язково врахуємо його при подальшому розвитку бота.",
        reply_markup=main_menu_kb(),
        parse_mode="Markdown",
    )

    if ADMIN_ID:
        user_label = f"@{user.username}" if user.username else (user.full_name or str(user.id))
        reply_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ Відповісти користувачу", callback_data=f"fb_reply_uid:{user.id}")]
        ])
        try:
            await bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    f"💬 **Новий відгук!**\n"
                    f"👤 Від: {user_label} (`{user.id}`)\n"
                    f"🕒 Час: {now_str}\n\n"
                    f"{feedback_text}"
                ),
                reply_markup=reply_kb,
                parse_mode="Markdown",
            )
        except Exception as e:
            logging.error(f"Не вдалося переслати відгук адміну {ADMIN_ID}: {e}")


@dp.callback_query(F.data.startswith("fb_reply_uid:"))
async def start_admin_reply_by_uid(callback: types.CallbackQuery, state: FSMContext):
    """Швидка відповідь прямо з миттєвого сповіщення про новий відгук."""
    if not ADMIN_ID or callback.from_user.id != ADMIN_ID:
        await callback.answer("Недостатньо прав", show_alert=True)
        return

    try:
        target_user_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Некоректний ID", show_alert=True)
        return

    info = users_data.get(str(target_user_id), {})
    who = _format_user_label(str(target_user_id), info)

    await state.set_state(FeedbackState.waiting_reply)
    await state.update_data(reply_to_user_id=target_user_id, reply_to_label=who)
    await callback.answer()
    await callback.message.answer(
        f"✍️ **Відповідь користувачу** {who}\n"
        f"Напишіть текст відповіді одним повідомленням:",
        reply_markup=cancel_feedback_kb(),
        parse_mode="Markdown",
    )


# Функція перевірки повітряної тривоги в Івано-Франківську (з резервом за структурою відповіді)
def check_ivano_frankivsk_alarm():
    try:
        resp = requests.get("https://alerts.in.ua/api/v1/alerts", timeout=6)
        if resp.status_code == 200:
            data = resp.json()
            entries = data.get("states") or data.get("alerts") or []
            if not isinstance(entries, list):
                return False
            for item in entries:
                if not isinstance(item, dict):
                    continue
                loc = str(
                    item.get("location_title")
                    or item.get("name")
                    or item.get("title")
                    or ""
                ).lower()
                if "івано-франк" in loc or "ivano-frank" in loc:
                    alert_type = str(item.get("alert_type") or item.get("type") or "air_raid").lower()
                    finished_at = item.get("finished_at") or item.get("end")
                    is_active_flag = item.get("active")
                    if alert_type in ("air_raid", "alert", "") and not finished_at and is_active_flag is not False:
                        return True
        else:
            logging.warning(f"API тривог повернув статус {resp.status_code}")
    except Exception as e:
        logging.error(f"Помилка перевірки тривоги: {e}")
    return False


def get_effective_alarm_status():
    """Повертає актуальний стан тривоги з урахуванням ручного перемикача адміністратора."""
    if manual_alarm_override is True:
        return True
    return check_ivano_frankivsk_alarm()


# Допоміжна функція для пошуку найближчого часу прибуття на зупинку
def get_next_arrival_info(dict_key, dir_id, stop_idx):
    now = datetime.now(KYIV_TZ)
    is_weekend = now.weekday() >= 5
    day_key = "weekends" if is_weekend else "workdays"

    route_data = schedules.get(dict_key, [])
    if not route_data or int(dir_id) >= len(route_data):
        return "Немає даних", "Невідомо"

    direction = route_data[int(dir_id)]
    stops = direction.get("stops", [])
    if int(stop_idx) >= len(stops):
        return "Немає даних", "Невідомо"

    offset = stops[int(stop_idx)]["offset"]
    dep_list = direction.get(day_key, [])

    if not dep_list:
        return "Розклад відсутній", "Невідомо"

    current_minutes = now.hour * 60 + now.minute
    next_arrival_str = None
    diff_minutes = None

    for dep_str in dep_list:
        h, m = map(int, dep_str.split(":"))
        dep_total_min = h * 60 + m
        arr_total_min = dep_total_min + offset

        if arr_total_min >= current_minutes:
            diff_minutes = arr_total_min - current_minutes
            arr_h = (arr_total_min // 60) % 24
            arr_m = arr_total_min % 60
            next_arrival_str = f"{arr_h:02d}:{arr_m:02d}"
            break

    if next_arrival_str is None and dep_list:
        h, m = map(int, dep_list[0].split(":"))
        arr_total_min = h * 60 + m + offset
        arr_h = (arr_total_min // 60) % 24
        arr_m = arr_total_min % 60
        next_arrival_str = f"{arr_h:02d}:{arr_m:02d} (завтра)"
        diff_minutes = (24 * 60 - current_minutes) + arr_total_min

    return next_arrival_str, diff_minutes


# 1. Головна кнопка «Маршрути»
@dp.message(F.text == "🚎 Маршрути")
async def choose_transport_type(message: types.Message, state: FSMContext):
    await state.clear()
    await record_user_activity(message.from_user)
    buttons = [
        [
            InlineKeyboardButton(text="🚎 Тролейбуси", callback_data="type:trolley"),
            InlineKeyboardButton(text="🚌 Автобуси", callback_data="type:bus")
        ]
    ]
    await message.answer("Оберіть вид транспорту:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("type:"))
async def choose_route_type(callback: types.CallbackQuery):
    t_type = callback.data.split(":")[1]
    buttons = []
    row = []

    if t_type == "trolley":
        route_keys = [k for k in schedules.keys() if not k.startswith("bus_")]
        title = "тролейбуса"
        prefix = "t"
    else:
        route_keys = [k.replace("bus_", "") for k in schedules.keys() if k.startswith("bus_")]
        title = "автобуса"
        prefix = "b"

    for route_num in route_keys:
        icon = "🚎" if t_type == "trolley" else "🚌"
        row.append(InlineKeyboardButton(text=f"{icon} №{route_num}", callback_data=f"route:{prefix}:{route_num}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    await callback.message.edit_text(f"Оберіть номер {title}:",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


@dp.callback_query(F.data.startswith("route:"))
async def choose_direction(callback: types.CallbackQuery):
    _, prefix, route_num = callback.data.split(":")
    dict_key = route_num if prefix == "t" else f"bus_{route_num}"
    directions = schedules.get(dict_key, [])
    icon = "🚎" if prefix == "t" else "🚌"

    buttons = []
    for d in directions:
        clean_name = d["direction"].replace(f"Тролейбус №{route_num}", "").replace(f"Автобус №{route_num}", "").strip()
        buttons.append(
            [InlineKeyboardButton(text=f"➡️ {clean_name}", callback_data=f"dir:{prefix}:{route_num}:{d['dir_id']}")])

    await callback.message.edit_text(
        f"{icon} Маршрут №{route_num}. Оберіть напрямок руху:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@dp.callback_query(F.data.startswith("dir:"))
async def choose_stop(callback: types.CallbackQuery):
    _, prefix, route_num, dir_id = callback.data.split(":")
    dict_key = route_num if prefix == "t" else f"bus_{route_num}"
    direction = schedules[dict_key][int(dir_id)]
    stops = direction["stops"]

    buttons = []
    for idx, stop in enumerate(stops):
        buttons.append([
            InlineKeyboardButton(
                text=f"{stop['name']} (+{stop['offset']} хв)",
                callback_data=f"stop:{prefix}:{route_num}:{dir_id}:{idx}",
            )
        ])

    buttons.extend([
        [
            InlineKeyboardButton(text="🗺 DozoR", url="https://city.dozor.tech/ua/iv-frankivsk/city"),
            InlineKeyboardButton(text="🚍 EasyWay", url="https://www.eway.in.ua/ua/cities/frankivsk")
        ],
        [
            InlineKeyboardButton(text="🚨 Мапа тривог", url="https://map.ukrainealarm.com/")
        ]
    ])

    await callback.message.edit_text(
        "Оберіть вашу зупинку або перевірте рух на карті:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@dp.message(RemindState.waiting_minutes, F.text & ~F.text.startswith("/"))
async def receive_custom_time(message: types.Message, state: FSMContext):
    text = message.text.strip()

    # Якщо це не число (наприклад, назва зупинки чи команда) — виходимо зі стану
    # і віддаємо повідомлення далі, на звичайний пошук зупинки
    if not (text.isascii() and text.isdigit()):
        await state.clear()
        raise SkipHandler

    minutes = int(text)
    if not (MIN_REMIND <= minutes <= MAX_REMIND):
        await message.answer(f"❌ Введіть число від {MIN_REMIND} до {MAX_REMIND}.")
        return

    data = await state.get_data()
    await state.clear()
    result = add_subscription(
        str(message.from_user.id),
        data["prefix"], data["route_num"], data["dir_id"], data["stop_idx"], minutes,
    )
    await message.answer(result, parse_mode="Markdown")


@dp.message(F.text & ~F.text.in_(MENU_BUTTONS) & ~F.text.startswith("/"))
async def search_stop_by_text(message: types.Message):
    await record_user_activity(message.from_user)
    query = message.text.strip().lower()
    found = []

    for r_key, directions in schedules.items():
        is_bus = r_key.startswith("bus_")
        route_num = r_key.replace("bus_", "")
        prefix = "b" if is_bus else "t"

        for dir_idx, direction in enumerate(directions):
            for stop_idx, stop in enumerate(direction.get("stops", [])):
                if query in stop["name"].lower():
                    found.append((prefix, route_num, str(dir_idx), stop_idx, stop["name"], direction["direction"]))

    if not found:
        await message.answer(
            "❌ Зупинку не знайдено. Спробуйте ввести іншу назву або скористайтесь кнопкою «🚎 Маршрути».",
            parse_mode="Markdown")
        return

    buttons = []
    for prefix, route_num, dir_id, stop_idx, stop_name, direction_text in found[:10]:
        icon = "🚌" if prefix == "b" else "🚎"
        buttons.append([
            InlineKeyboardButton(
                text=f"{icon} №{route_num} — {stop_name}",
                callback_data=f"stop:{prefix}:{route_num}:{dir_id}:{stop_idx}"
            )
        ])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer(f"🔍 Знайдено зупинки за запитом «{message.text}»:", reply_markup=kb)


@dp.callback_query(F.data.startswith("stop:"))
async def choose_remind_time(callback: types.CallbackQuery):
    _, prefix, route_num, dir_id, stop_idx = callback.data.split(":")
    buttons = [
        [
            InlineKeyboardButton(text="За 5 хв", callback_data=f"save:{prefix}:{route_num}:{dir_id}:{stop_idx}:5"),
            InlineKeyboardButton(text="За 10 хв", callback_data=f"save:{prefix}:{route_num}:{dir_id}:{stop_idx}:10"),
            InlineKeyboardButton(text="За 15 хв", callback_data=f"save:{prefix}:{route_num}:{dir_id}:{stop_idx}:15"),
        ],
        [
            InlineKeyboardButton(text="✏️ Своє число (1–60)", callback_data=f"custom:{prefix}:{route_num}:{dir_id}:{stop_idx}"),
        ],
    ]
    await callback.message.answer(
        "За скільки хвилин до прибуття на зупинку вас попереджати?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("custom:"))
async def ask_custom_time(callback: types.CallbackQuery, state: FSMContext):
    _, prefix, route_num, dir_id, stop_idx = callback.data.split(":")
    await state.set_state(RemindState.waiting_minutes)
    await state.update_data(prefix=prefix, route_num=route_num, dir_id=dir_id, stop_idx=stop_idx)
    await callback.message.answer(
        f"✏️ Введіть число хвилин від {MIN_REMIND} до {MAX_REMIND} — за скільки попереджати про прибуття:"
    )
    await callback.answer()


def add_subscription(user_id, prefix, route_num, dir_id, stop_idx, remind_before):
    """Зберігає підписку та повертає текст підтвердження."""
    dict_key = route_num if prefix == "t" else f"bus_{route_num}"
    direction = schedules[dict_key][int(dir_id)]
    stop = direction["stops"][int(stop_idx)]

    next_time, diff_min = get_next_arrival_info(dict_key, dir_id, stop_idx)
    time_info_str = f"🕒 Найближчий рейс: о **{next_time}** (через ~{diff_min} хв)" if isinstance(diff_min, int) else f"🕒 Найближчий рейс: {next_time}"

    user_subs = subscriptions.setdefault(user_id, [])
    user_subs.append({
        "prefix": prefix,
        "route": route_num,
        "dir_id": dir_id,
        "stop_idx": int(stop_idx),
        "remind_before": int(remind_before),
        "active": True,
    })
    storage.save_subscriptions(subscriptions)

    icon = "🚎" if prefix == "t" else "🚌"
    return (
        f"✅ **Збережено!**\n"
        f"{icon} Маршрут №{route_num}\n"
        f"📍 Зупинка: **{stop['name']}**\n"
        f"{time_info_str}\n"
        f"⏰ Нагадування: за **{remind_before} хв** (+ буфер на дорогу) до прибуття.\n\n"
        f"Керувати збереженими маршрутами можна у меню «🔔 Мої зупинки»."
    )


@dp.callback_query(F.data.startswith("save:"))
async def save_subscription(callback: types.CallbackQuery):
    _, prefix, route_num, dir_id, stop_idx, remind_before = callback.data.split(":")
    result = add_subscription(str(callback.from_user.id), prefix, route_num, dir_id, stop_idx, remind_before)
    await callback.message.edit_text(result, parse_mode="Markdown")


# --- Перегляд та керування збереженими зупинками ---
def build_subscriptions_view(user_id: str):
    """Повертає (текст, клавіатура) зі списком підписок конкретного користувача."""
    user_subs = subscriptions.get(user_id, [])
    if not user_subs:
        return "У вас ще немає збережених зупинок. Натисніть «🚎 Маршрути», щоб додати.", None

    text = "Ваші збережені маршрути та зупинки:\n\n"
    buttons = []

    # Загальна кнопка керування всіма сповіщеннями
    any_active = any(sub.get("active", True) for sub in user_subs)
    if any_active:
        buttons.append([InlineKeyboardButton(text="⏸ Вимкнути всі сповіщення", callback_data="toggle:all")])
    else:
        buttons.append([InlineKeyboardButton(text="▶️ Увімкнути всі сповіщення", callback_data="toggle:all")])

    for idx, sub in enumerate(user_subs):
        prefix = sub.get("prefix", "t")
        route_num = str(sub["route"])
        dir_id = int(sub["dir_id"])
        stop_idx = int(sub["stop_idx"])

        dict_key = route_num if prefix == "t" else f"bus_{route_num}"
        d = schedules[dict_key][dir_id]
        stop = d["stops"][stop_idx]
        status = "🟢 Увімкнено" if sub.get("active", True) else "⏸ На паузі"
        icon = "🚎" if prefix == "t" else "🚌"

        next_time, diff_min = get_next_arrival_info(dict_key, dir_id, stop_idx)
        live_status = f"прибуде о {next_time} (через {diff_min} хв)" if isinstance(diff_min, int) else f"{next_time}"

        text += (
            f"{idx + 1}. {icon} Маршрут №{route_num} — {stop['name']}\n"
            f"   ⏳ Наступний: **{live_status}**\n"
            f"   Сповіщення: за {sub['remind_before']} хв ({status})\n\n"
        )
        toggle_label = "⏸ Пауза" if sub.get("active", True) else "▶️ Увімкнути"
        buttons.append([
            InlineKeyboardButton(text=f"{toggle_label} {icon} №{route_num}", callback_data=f"toggle:{idx}"),
            InlineKeyboardButton(text=f"🗑 Видалити {icon} №{route_num}", callback_data=f"del:{idx}"),
        ])

    buttons.extend([
        [
            InlineKeyboardButton(text="🗺 DozoR (Карта)", url="https://city.dozor.tech/ua/iv-frankivsk/city"),
            InlineKeyboardButton(text="🚍 EasyWay", url="https://www.eway.in.ua/ua/cities/frankivsk")
        ],
        [
            InlineKeyboardButton(text="🚨 Мапа тривог", url="https://map.ukrainealarm.com/")
        ]
    ])
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


async def show_subscriptions(user_id: str, message: types.Message, edit: bool = False):
    """user_id передаємо окремо: у callback.message відправник — це сам бот, а не користувач."""
    try:
        text, kb = build_subscriptions_view(user_id)
        if edit:
            await message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
        else:
            await message.answer(text, reply_markup=kb, parse_mode="Markdown")
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            logging.error(f"Помилка Telegram у моїх зупинках: {e}")
    except Exception as e:
        logging.error(f"Помилка в моїх зупинках: {e}")
        await message.answer(
            "Сталася помилка при завантаженні вашого списку зупинок. Спробуйте зберегти зупинку заново через «🚎 Маршрути».")


@dp.message(F.text == "🔔 Мої зупинки")
async def my_subscriptions(message: types.Message, state: FSMContext):
    await state.clear()
    await record_user_activity(message.from_user)
    await show_subscriptions(str(message.from_user.id), message)


@dp.callback_query(F.data.startswith("toggle:"))
async def toggle_sub(callback: types.CallbackQuery):
    target = callback.data.split(":")[1]
    user_id = str(callback.from_user.id)

    if user_id not in subscriptions or not subscriptions[user_id]:
        await callback.answer("У вас немає збережених зупинок!")
        return

    if target == "all":
        any_active = any(sub.get("active", True) for sub in subscriptions[user_id])
        new_state = not any_active
        for sub in subscriptions[user_id]:
            sub["active"] = new_state
        storage.save_subscriptions(subscriptions)
        await callback.answer("Всі сповіщення увімкнено! 🟢" if new_state else "Всі сповіщення на паузі! ⏸")
    else:
        idx = int(target)
        if idx < len(subscriptions[user_id]):
            curr = subscriptions[user_id][idx].get("active", True)
            subscriptions[user_id][idx]["active"] = not curr
            storage.save_subscriptions(subscriptions)
        await callback.answer("Статус змінено!")

    await show_subscriptions(user_id, callback.message, edit=True)


@dp.callback_query(F.data.startswith("del:"))
async def delete_sub(callback: types.CallbackQuery):
    idx = int(callback.data.split(":")[1])
    user_id = str(callback.from_user.id)
    if user_id in subscriptions and idx < len(subscriptions[user_id]):
        subscriptions[user_id].pop(idx)
        storage.save_subscriptions(subscriptions)
    await callback.answer("Видалено!")
    await show_subscriptions(user_id, callback.message, edit=True)


# --- Фонова перевірка розкладу щохвилини (з урахуванням тривоги та відбою) ---
async def check_schedules():
    global last_all_clear_time, last_effective_alarm_state, was_real_alarm
    now = datetime.now(KYIV_TZ)

    # Перевіряємо чи є зараз реальна тривога через API та враховуємо ручний режим
    api_alarm = check_ivano_frankivsk_alarm()
    if api_alarm:
        was_real_alarm = True

    is_alarm = True if manual_alarm_override is True else api_alarm
    last_effective_alarm_state = is_alarm

    for user_id in list(subscriptions.keys()):
        current_alarm_state = user_alarm_status.get(user_id, False)

        if is_alarm:
            if not current_alarm_state:
                user_alarm_status[user_id] = True
                try:
                    alarm_kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🚨 Мапа тривог", url="https://map.ukrainealarm.com/")]
                    ])
                    await bot.send_message(
                        chat_id=int(user_id),
                        text=(
                            "🔴 **Увага! В місті Івано-Франківськ повітряна тривога (червоний рівень небезпеки).**\n"
                            "Нагадування про вихід на зупинку тимчасово призупинено до відбою."
                        ),
                        reply_markup=alarm_kb,
                        parse_mode="Markdown",
                    )
                except Exception as e:
                    logging.error(f"Не вдалося надіслати сповіщення про тривогу {user_id}: {e}")
            continue
        else:
            if current_alarm_state:
                user_alarm_status[user_id] = False
                if was_real_alarm:
                    last_all_clear_time = now
                    was_real_alarm = False
                    try:
                        live_kb = InlineKeyboardMarkup(inline_keyboard=[
                            [
                                InlineKeyboardButton(text="🗺 DozoR (Наживо)", url="https://city.dozor.tech/ua/iv-frankivsk/city"),
                                InlineKeyboardButton(text="🚍 EasyWay", url="https://www.eway.in.ua/ua/cities/frankivsk")
                            ]
                        ])
                        await bot.send_message(
                            chat_id=int(user_id),
                            text=(
                                "🟢 **Відбій повітряної тривоги!**\n"
                                "⚠️ Протягом найближчої години транспорт може бути відхилений від графіка "
                                "(згодом рух поступово стабілізується). Рекомендуємо звірятися з онлайн-картою:"
                            ),
                            reply_markup=live_kb,
                            parse_mode="Markdown",
                        )
                    except Exception as e:
                        logging.error(f"Не вдалося надіслати сповіщення про відбій {user_id}: {e}")
                else:
                    try:
                        await bot.send_message(
                            chat_id=int(user_id),
                            text="🟢 **Тренувальний режим тривоги завершено.** Сповіщення про зупинки працюють у звичайному режимі.",
                            parse_mode="Markdown",
                        )
                    except Exception as e:
                        logging.error(f"Не вдалося надіслати сповіщення про завершення тренування {user_id}: {e}")

    if is_alarm:
        return

    # Перевіряємо, чи не минуло менше 60 хвилин після останнього реального відбою
    is_post_alarm = (
        last_all_clear_time is not None
        and (now - last_all_clear_time) < timedelta(hours=1)
    )

    is_weekend = now.weekday() >= 5
    day_key = "weekends" if is_weekend else "workdays"

    for user_id, user_subs in list(subscriptions.items()):
        for sub in list(user_subs):
            if not sub.get("active", True):
                continue

            prefix = sub.get("prefix", "t")
            route_num = str(sub["route"])
            dict_key = route_num if prefix == "t" else f"bus_{route_num}"

            # Одна «битa» підписка не повинна ламати сповіщення всім іншим
            try:
                route_data = schedules.get(dict_key, [])[int(sub["dir_id"])]
                stop_info = route_data["stops"][int(sub["stop_idx"])]
            except (IndexError, KeyError, ValueError, TypeError) as e:
                logging.error(f"Некоректна підписка {user_id}: {sub} ({e})")
                continue
            offset = stop_info["offset"]

            # Буфер безпеки: +2 хвилини наперед
            safety_buffer = 2
            remind_before = sub["remind_before"] + safety_buffer

            for dep_str in route_data.get(day_key, []):
                hour, minute = map(int, dep_str.split(":"))
                dep_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                arrival_time = dep_time + timedelta(minutes=offset)
                notify_time = arrival_time - timedelta(minutes=remind_before)

                if now.hour == notify_time.hour and now.minute == notify_time.minute:
                    arr_str = arrival_time.strftime("%H:%M")
                    icon = "🚎" if prefix == "t" else "🚌"

                    msg_text = (
                        f"⚠️ **Час виходити на зупинку!**\n"
                        f"{icon} **Маршрут №{route_num}** буде на зупинці **{stop_info['name']}** "
                        f"приблизно о **{arr_str}** (з урахуванням запасу часу на дорогу)."
                    )
                    reply_kb = None

                    # Протягом 1 години після реального відбою попереджаємо про можливе відхилення від графіка
                    if is_post_alarm:
                        msg_text += (
                            "\n\n🚨 *Увага: після недавньої тривоги транспорт може бути відхилений від графіка "
                            "(прибувати раніше або пізніше), але згодом рух поступово стабілізується. "
                            "Перевірте GPS на карті:*"
                        )
                        reply_kb = InlineKeyboardMarkup(inline_keyboard=[
                            [
                                InlineKeyboardButton(text="🗺 DozoR", url="https://city.dozor.tech/ua/iv-frankivsk/city"),
                                InlineKeyboardButton(text="🚍 EasyWay", url="https://www.eway.in.ua/ua/cities/frankivsk")
                            ]
                        ])

                    try:
                        await bot.send_message(
                            chat_id=int(user_id),
                            text=msg_text,
                            reply_markup=reply_kb,
                            parse_mode="Markdown",
                        )
                    except Exception as e:
                        logging.error(f"Не вдалося надіслати повідомлення {user_id}: {e}")


async def main():
    scheduler = AsyncIOScheduler(timezone=KYIV_TZ)
    scheduler.add_job(check_schedules, "cron", second=5)
    scheduler.start()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())