import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
import pytz

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from apscheduler.schedulers.asyncio import AsyncIOScheduler

BOT_TOKEN = "6241822896:AAHs62SrJ6RgLv58QXqvf32AQWSNRUyWXlw"
KYIV_TZ = pytz.timezone("Europe/Kyiv")
SCHEDULES_FILE = "schedules.json"
SUBS_FILE = "subscriptions.json"

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


def load_json(filename, default):
    if os.path.exists(filename):
        with open(filename, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(filename, data):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


schedules = load_json(SCHEDULES_FILE, {})
subscriptions = load_json(SUBS_FILE, {})


def main_menu_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🚎 Маршрути"), KeyboardButton(text="🔔 Мої зупинки")],
        ],
        resize_keyboard=True,
    )


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(
        "Привіт! Я бот розкладу тролейбусів Івано-Франківська.\n"
        "Натисни **«🚎 Маршрути»** або просто напиши назву своєї зупинки текстом (наприклад: *Космос*, *Лепкого*), і я знайду її розклад!",
        reply_markup=main_menu_kb(),
        parse_mode="Markdown",
    )


# 1. Вибір номера маршруту при натисканні «Маршрути»
@dp.message(F.text == "🚎 Маршрути")
async def choose_route(message: types.Message):
    buttons = []
    row = []
    for route_num in schedules.keys():
        row.append(InlineKeyboardButton(text=f"Тролейбус №{route_num}", callback_data=f"route:{route_num}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    await message.answer("Оберіть номер тролейбуса:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


# 2. Вибір напрямку
@dp.callback_query(F.data.startswith("route:"))
async def choose_direction(callback: types.CallbackQuery):
    route_num = callback.data.split(":")[1]
    directions = schedules.get(route_num, [])

    buttons = []
    for d in directions:
        clean_name = d["direction"].replace(f"Тролейбус №{route_num}", "").strip()
        buttons.append([InlineKeyboardButton(text=f"➡️ {clean_name}", callback_data=f"dir:{route_num}:{d['dir_id']}")])

    await callback.message.edit_text(
        f"Тролейбус №{route_num}. Оберіть напрямок руху:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


# 3. Вибір зупинки з кнопок
@dp.callback_query(F.data.startswith("dir:"))
async def choose_stop(callback: types.CallbackQuery):
    _, route_num, dir_id = callback.data.split(":")
    direction = schedules[route_num][int(dir_id)]
    stops = direction["stops"]

    buttons = []
    for idx, stop in enumerate(stops):
        buttons.append([
            InlineKeyboardButton(
                text=f"{stop['name']} (+{stop['offset']} хв)",
                callback_data=f"stop:{route_num}:{dir_id}:{idx}",
            )
        ])

    await callback.message.edit_text(
        "Оберіть вашу зупинку:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


# 4. Пошук зупинки за текстовим запитом користувача
@dp.message(F.text & ~F.text.in_(["🚎 Маршрути", "🔔 Мої зупинки"]))
async def search_stop_by_text(message: types.Message):
    query = message.text.strip().lower()
    found = []

    for route_num, directions in schedules.items():
        for dir_idx, direction in enumerate(directions):
            for stop_idx, stop in enumerate(direction.get("stops", [])):
                if query in stop["name"].lower():
                    found.append((route_num, str(dir_idx), stop_idx, stop["name"], direction["direction"]))

    if not found:
        await message.answer("❌ Зупинку не знайдено. Спробуйте ввести іншу назву (наприклад: *Космос*, *Вокзал*) або скористайтесь кнопкою «🚎 Маршрути».", parse_mode="Markdown")
        return

    buttons = []
    for route_num, dir_id, stop_idx, stop_name, direction_text in found[:10]:
        safe_dir = direction_text.replace(f"Тролейбус №{route_num}", "").strip()
        buttons.append([
            InlineKeyboardButton(
                text=f"🚎 №{route_num} — {stop_name} ({safe_dir[:20]})",
                callback_data=f"stop:{route_num}:{dir_id}:{stop_idx}"
            )
        ])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer(f"🔍 Знайдено зупинки за запитом «{message.text}»:", reply_markup=kb)


# 5. Вибір часу нагадування та збереження
@dp.callback_query(F.data.startswith("stop:"))
async def choose_remind_time(callback: types.CallbackQuery):
    _, route_num, dir_id, stop_idx = callback.data.split(":")
    buttons = [
        [
            InlineKeyboardButton(text="За 5 хв", callback_data=f"save:{route_num}:{dir_id}:{stop_idx}:5"),
            InlineKeyboardButton(text="За 10 хв", callback_data=f"save:{route_num}:{dir_id}:{stop_idx}:10"),
            InlineKeyboardButton(text="За 15 хв", callback_data=f"save:{route_num}:{dir_id}:{stop_idx}:15"),
        ]
    ]
    await callback.message.answer(
        "За скільки хвилин до прибуття на зупинку вас попереджати?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("save:"))
async def save_subscription(callback: types.CallbackQuery):
    _, route_num, dir_id, stop_idx, remind_before = callback.data.split(":")
    user_id = str(callback.from_user.id)

    direction = schedules[route_num][int(dir_id)]
    stop = direction["stops"][int(stop_idx)]

    user_subs = subscriptions.setdefault(user_id, [])
    new_sub = {
        "route": route_num,
        "dir_id": dir_id,
        "stop_idx": int(stop_idx),
        "remind_before": int(remind_before),
        "active": True,
    }
    user_subs.append(new_sub)
    save_json(SUBS_FILE, subscriptions)

    await callback.message.edit_text(
        f"✅ **Збережено!**\n"
        f"🚎 Тролейбус №{route_num}\n"
        f"📍 Зупинка: **{stop['name']}**\n"
        f"⏰ Нагадування: за **{remind_before} хв** до прибуття.\n\n"
        f"Керувати збереженими маршрутами можна у меню «🔔 Мої зупинки».",
        parse_mode="Markdown",
    )


# --- Перегляд та керування збереженими зупинками ---
@dp.message(F.text == "🔔 Мої зупинки")
async def my_subscriptions(message: types.Message):
    user_id = str(message.from_user.id)
    user_subs = subscriptions.get(user_id, [])

    if not user_subs:
        await message.answer("У вас ще немає збережених зупинок. Натисніть «🚎 Маршрути», щоб додати.")
        return

    try:
        text = "**Ваші збережені маршрути та зупинки:**\n\n"
        buttons = []
        for idx, sub in enumerate(user_subs):
            route_num = str(sub["route"])
            dir_id = int(sub["dir_id"])
            stop_idx = int(sub["stop_idx"])

            d = schedules[route_num][dir_id]
            stop = d["stops"][stop_idx]
            status = "🟢 Увімкнено" if sub.get("active", True) else "⏸ На паузі"

            text += (
                f"{idx + 1}. **Тролейбус №{route_num}** — {stop['name']}\n"
                f"   Напрямок: {d['direction']}\n"
                f"   Сповіщення: за {sub['remind_before']} хв ({status})\n\n"
            )
            toggle_label = "⏸ Пауза" if sub.get("active", True) else "▶️ Увімкнути"
            buttons.append([
                InlineKeyboardButton(text=f"{toggle_label} #{idx + 1}", callback_data=f"toggle:{idx}"),
                InlineKeyboardButton(text=f"🗑 Видалити #{idx + 1}", callback_data=f"del:{idx}"),
            ])

        await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Помилка в моїх зупинках: {e}")
        await message.answer(
            "Сталася помилка при завантаженні вашого списку зупинок. Спробуйте зберегти зупинку заново через «🚎 Маршрути».")


@dp.callback_query(F.data.startswith("toggle:"))
async def toggle_sub(callback: types.CallbackQuery):
    idx = int(callback.data.split(":")[1])
    user_id = str(callback.from_user.id)
    if user_id in subscriptions and idx < len(subscriptions[user_id]):
        curr = subscriptions[user_id][idx].get("active", True)
        subscriptions[user_id][idx]["active"] = not curr
        save_json(SUBS_FILE, subscriptions)
    await callback.answer("Статус змінено!")
    await my_subscriptions(callback.message)


@dp.callback_query(F.data.startswith("del:"))
async def delete_sub(callback: types.CallbackQuery):
    idx = int(callback.data.split(":")[1])
    user_id = str(callback.from_user.id)
    if user_id in subscriptions and idx < len(subscriptions[user_id]):
        subscriptions[user_id].pop(idx)
        save_json(SUBS_FILE, subscriptions)
    await callback.answer("Видалено!")
    await my_subscriptions(callback.message)


# --- Фонова перевірка розкладу щохвилини ---
async def check_schedules():
    now = datetime.now(KYIV_TZ)
    is_weekend = now.weekday() >= 5
    day_key = "weekends" if is_weekend else "workdays"

    for user_id, user_subs in subscriptions.items():
        for sub in user_subs:
            if not sub.get("active", True):
                continue

            route_data = schedules.get(sub["route"], [])[int(sub["dir_id"])]
            stop_info = route_data["stops"][sub["stop_idx"]]
            offset = stop_info["offset"]
            remind_before = sub["remind_before"]

            for dep_str in route_data.get(day_key, []):
                hour, minute = map(int, dep_str.split(":"))
                dep_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                arrival_time = dep_time + timedelta(minutes=offset)
                notify_time = arrival_time - timedelta(minutes=remind_before)

                if now.hour == notify_time.hour and now.minute == notify_time.minute:
                    arr_str = arrival_time.strftime("%H:%M")
                    try:
                        await bot.send_message(
                            chat_id=int(user_id),
                            text=(
                                f"🚎 **Тролейбус №{sub['route']}** буде на зупинці **{stop_info['name']}** "
                                f"через **{remind_before} хв** (о **{arr_str}**)."
                            ),
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