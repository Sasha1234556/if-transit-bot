"""Зберігання підписок користувачів.

Якщо задано змінну середовища DATABASE_URL — дані лежать у Postgres (наприклад, Neon)
і переживають будь-які деплої та перезапуски.
Якщо ні — використовується локальний файл subscriptions.json (зручно для запуску на своєму ПК).
"""
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor

DATABASE_URL = os.environ.get("DATABASE_URL")
LOCAL_FILE = "subscriptions.json"
KEY = "subscriptions"

# Один потік для запису: збереження не блокує бота (Neon після простою "прокидається"
# кілька секунд), а порядок записів зберігається.
_executor = ThreadPoolExecutor(max_workers=1)


def _read_local():
    if os.path.exists(LOCAL_FILE):
        with open(LOCAL_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _write_local(payload: str):
    with open(LOCAL_FILE, "w", encoding="utf-8") as f:
        f.write(payload)


def _run(fn):
    import psycopg2

    conn = psycopg2.connect(DATABASE_URL, connect_timeout=15)
    try:
        with conn:  # commit / rollback
            with conn.cursor() as cur:
                return fn(cur)
    finally:
        conn.close()


def _upsert(cur, payload: str):
    cur.execute(
        "INSERT INTO bot_data (key, value) VALUES (%s, %s::jsonb) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (KEY, payload),
    )


def load_subscriptions() -> dict:
    if not DATABASE_URL:
        logging.warning(
            "DATABASE_URL не задано: підписки зберігаються у локальному файлі "
            "(на Render вони зникнуть після перезапуску!)"
        )
        return _read_local()

    # Помилку навмисно НЕ ковтаємо: якщо база недоступна, краще впасти при старті,
    # ніж запуститися з порожнім списком і потім затерти ним дані в базі.
    def work(cur):
        cur.execute("CREATE TABLE IF NOT EXISTS bot_data (key TEXT PRIMARY KEY, value JSONB NOT NULL)")
        cur.execute("SELECT value FROM bot_data WHERE key = %s", (KEY,))
        row = cur.fetchone()
        if row is not None:
            return row[0], False
        # Перший запуск з базою: переносимо підписки з файлу, якщо він є
        data = _read_local()
        _upsert(cur, json.dumps(data, ensure_ascii=False))
        return data, True

    data, seeded = _run(work)
    if seeded:
        logging.info(f"Підписки імпортовано з {LOCAL_FILE} у базу даних (користувачів: {len(data)})")
    else:
        logging.info(f"Підписки завантажено з бази даних (користувачів: {len(data)})")
    return data


def save_subscriptions(data: dict) -> None:
    # Знімок робимо одразу, щоб подальші зміни в пам'яті не вплинули на запис
    payload = json.dumps(data, ensure_ascii=False, indent=2)

    def work():
        if DATABASE_URL:
            try:
                _run(lambda cur: _upsert(cur, payload))
                return
            except Exception as e:
                logging.error(f"Не вдалося зберегти підписки в базу: {e}. Записую у локальний файл.")
        _write_local(payload)

    _executor.submit(work)