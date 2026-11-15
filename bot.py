import web
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

# Клавіатура з посиланнями на живі карти Івано-Франківська
map_keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="🗺 DozoR (Карта)", url="https://city.dozor.tech/ua/iv-frankivsk/city"),
        InlineKeyboardButton(text="🚍 EasyWay", url="https://www.eway.in.ua/ua/cities/frankivsk")
    ]
])


def main_menu_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🚎 Маршрути"), KeyboardButton(text="🔔 Мої зупинки")],
            [KeyboardButton(text="🗺 Онлайн-карта")]
        ],
        resize_keyboard=True,
    )


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(
        "Привіт! Я бот розкладу тролейбусів та автобусів Івано-Франківська.\n"
        "Натисни **«🚎 Маршрути»**, скористайся кнопкою карти внизу або просто напиши назву своєї зупинки текстом, і я знайду її розклад!",
        reply_markup=main_menu_kb(),
        parse_mode="Markdown",
    )


@dp.message(F.text == "🗺 Онлайн-карта")
async def open_map_links(message: types.Message):
    await message.answer(
        "Оберіть сервіс для перегляду транспорту наживо:",
        reply_markup=map_keyboard
    )


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

    # Якщо на сьогодні рейси скінчилися, беремо перший рейс наступного дня
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
async def choose_transport_type(message: types.Message):
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

    buttons.append([
        InlineKeyboardButton(text="🗺 DozoR", url="https://city.dozor.tech/ua/iv-frankivsk/city"),
        InlineKeyboardButton(text="🚍 EasyWay", url="https://www.eway.in.ua/ua/cities/frankivsk")
    ])

    await callback.message.edit_text(
        "Оберіть вашу зупинку або перевірте рух на карті:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@dp.message(F.text & ~F.text.in_(["🚎 Маршрути", "🔔 Мої зупинки", "🗺 Онлайн-карта"]))
async def search_stop_by_text(message: types.Message):
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
        ]
    ]
    await callback.message.answer(
        "За скільки хвилин до прибуття на зупинку вас попереджати?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("save:"))
async def save_subscription(callback: types.CallbackQuery):
    _, prefix, route_num, dir_id, stop_idx, remind_before = callback.data.split(":")
    user_id = str(callback.from_user.id)

    dict_key = route_num if prefix == "t" else f"bus_{route_num}"
    direction = schedules[dict_key][int(dir_id)]
    stop = direction["stops"][int(stop_idx)]

    # Вираховуємо найближчий рейс прямо зараз
    next_time, diff_min = get_next_arrival_info(dict_key, dir_id, stop_idx)
    time_info_str = f"🕒 Найближчий рейс: о **{next_time}** (через ~{diff_min} хв)" if isinstance(diff_min,
                                                                                                 int) else f"🕒 Найближчий рейс: {next_time}"

    user_subs = subscriptions.setdefault(user_id, [])
    new_sub = {
        "prefix": prefix,
        "route": route_num,
        "dir_id": dir_id,
        "stop_idx": int(stop_idx),
        "remind_before": int(remind_before),
        "active": True,
    }
    user_subs.append(new_sub)
    save_json(SUBS_FILE, subscriptions)

    icon = "🚎" if prefix == "t" else "🚌"
    await callback.message.edit_text(
        f"✅ **Збережено!**\n"
        f"{icon} Маршрут №{route_num}\n"
        f"📍 Зупинка: **{stop['name']}**\n"
        f"{time_info_str}\n"
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
        text = "Ваші збережені маршрути та зупинки:\n\n"
        buttons = []
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

            # Отримуємо найближчий час прибуття для кожної збереженої зупинки
            next_time, diff_min = get_next_arrival_info(dict_key, dir_id, stop_idx)
            live_status = f"прибуде о {next_time} (через {diff_min} хв)" if isinstance(diff_min,
                                                                                       int) else f"{next_time}"

            text += (
                f"{idx + 1}. {icon} Маршрут №{route_num} — {stop['name']}\n"
                f"   ⏳ Наступний: **{live_status}**\n"
                f"   Сповіщення: за {sub['remind_before']} хв ({status})\n\n"
            )
            toggle_label = "⏸ Пауза" if sub.get("active", True) else "▶️ Увімкнути"
            buttons.append([
                InlineKeyboardButton(text=f"{toggle_label} #{idx + 1}", callback_data=f"toggle:{idx}"),
                InlineKeyboardButton(text=f"🗑 Видалити #{idx + 1}", callback_data=f"del:{idx}"),
            ])

        buttons.append([
            InlineKeyboardButton(text="🗺 DozoR (Карта)", url="https://city.dozor.tech/ua/iv-frankivsk/city"),
            InlineKeyboardButton(text="🚍 EasyWay", url="https://www.eway.in.ua/ua/cities/frankivsk")
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

            prefix = sub.get("prefix", "t")
            route_num = str(sub["route"])
            dict_key = route_num if prefix == "t" else f"bus_{route_num}"

            route_data = schedules.get(dict_key, [])[int(sub["dir_id"])]
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
                    icon = "🚎" if prefix == "t" else "🚌"
                    try:
                        await bot.send_message(
                            chat_id=int(user_id),
                            text=(
                                f"{icon} **Маршрут №{route_num}** буде на зупинці **{stop_info['name']}** "
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