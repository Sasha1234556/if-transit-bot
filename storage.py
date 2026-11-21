"""Зберігання підписок, відгуків та користувачів бота.

Якщо задано змінну середовища DATABASE_URL — дані лежать у Postgres (наприклад, Neon)
і переживають будь-які деплої та перезапуски.
Якщо ні — використовується локальний файл subscriptions.json / feedbacks.json / users.json
(зручно для запуску на своєму ПК).
"""
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor

DATABASE_URL = os.environ.get("DATABASE_URL")
LOCAL_FILE = "subscriptions.json"
FEEDBACK_LOCAL_FILE = "feedbacks.json"
USERS_LOCAL_FILE = "users.json"
EWAY_LOCAL_FILE = "eway_state.json"
KEY = "subscriptions"
FEEDBACK_KEY = "feedbacks"
USERS_KEY = "users"
EWAY_KEY = "eway_state"

# Один потік для запису: збереження не блокує бота (Neon після простою "прокидається"
# кілька секунд), а порядок записів зберігається.
_executor = ThreadPoolExecutor(max_workers=1)


def _read_local(filename=LOCAL_FILE, default=None):
    if default is None:
        default = {}
    if os.path.exists(filename):
        with open(filename, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def _write_local(payload: str, filename=LOCAL_FILE):
    with open(filename, "w", encoding="utf-8") as f:
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


def _upsert(cur, payload: str, key: str = KEY):
    cur.execute(
        "INSERT INTO bot_data (key, value) VALUES (%s, %s::jsonb) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (key, payload),
    )


def load_subscriptions() -> dict:
    if not DATABASE_URL:
        logging.warning(
            "DATABASE_URL не задано: підписки зберігаються у локальному файлі "
            "(на Render вони зникнуть після перезапуску!)"
        )
        return _read_local(LOCAL_FILE, {})

    # Помилку навмисно НЕ ковтаємо: якщо база недоступна, краще впасти при старті,
    # ніж запуститися з порожнім списком і потім затерти ним дані в базі.
    def work(cur):
        cur.execute("CREATE TABLE IF NOT EXISTS bot_data (key TEXT PRIMARY KEY, value JSONB NOT NULL)")
        cur.execute("SELECT value FROM bot_data WHERE key = %s", (KEY,))
        row = cur.fetchone()
        if row is not None:
            return row[0], False
        # Перший запуск з базою: переносимо підписки з файлу, якщо він є
        data = _read_local(LOCAL_FILE, {})
        _upsert(cur, json.dumps(data, ensure_ascii=False), KEY)
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
                _run(lambda cur: _upsert(cur, payload, KEY))
                return
            except Exception as e:
                logging.error(f"Не вдалося зберегти підписки в базу: {e}. Записую у локальний файл.")
        _write_local(payload, LOCAL_FILE)

    _executor.submit(work)


def load_feedbacks() -> list:
    if not DATABASE_URL:
        return _read_local(FEEDBACK_LOCAL_FILE, [])

    def work(cur):
        cur.execute("CREATE TABLE IF NOT EXISTS bot_data (key TEXT PRIMARY KEY, value JSONB NOT NULL)")
        cur.execute("SELECT value FROM bot_data WHERE key = %s", (FEEDBACK_KEY,))
        row = cur.fetchone()
        if row is not None and isinstance(row[0], list):
            return row[0]
        return _read_local(FEEDBACK_LOCAL_FILE, [])

    try:
        return _run(work)
    except Exception as e:
        logging.error(f"Не вдалося завантажити відгуки з бази: {e}")
        return _read_local(FEEDBACK_LOCAL_FILE, [])


def save_feedback(entry: dict) -> None:
    def work():
        current = load_feedbacks()
        current.append(entry)
        payload = json.dumps(current, ensure_ascii=False, indent=2)
        if DATABASE_URL:
            try:
                _run(lambda cur: _upsert(cur, payload, FEEDBACK_KEY))
                return
            except Exception as e:
                logging.error(f"Не вдалося зберегти відгук у базу: {e}. Записую у локальний файл.")
        _write_local(payload, FEEDBACK_LOCAL_FILE)

    _executor.submit(work)


def load_users() -> dict:
    if not DATABASE_URL:
        return _read_local(USERS_LOCAL_FILE, {})

    def work(cur):
        cur.execute("CREATE TABLE IF NOT EXISTS bot_data (key TEXT PRIMARY KEY, value JSONB NOT NULL)")
        cur.execute("SELECT value FROM bot_data WHERE key = %s", (USERS_KEY,))
        row = cur.fetchone()
        if row is not None and isinstance(row[0], dict):
            return row[0]
        return _read_local(USERS_LOCAL_FILE, {})

    try:
        return _run(work)
    except Exception as e:
        logging.error(f"Не вдалося завантажити користувачів з бази: {e}")
        return _read_local(USERS_LOCAL_FILE, {})


def save_users(data: dict) -> None:
    payload = json.dumps(data, ensure_ascii=False, indent=2)

    def work():
        if DATABASE_URL:
            try:
                _run(lambda cur: _upsert(cur, payload, USERS_KEY))
                return
            except Exception as e:
                logging.error(f"Не вдалося зберегти користувачів у базу: {e}. Записую у локальний файл.")
        _write_local(payload, USERS_LOCAL_FILE)

    _executor.submit(work)


def load_eway_state() -> dict:
    default_state = {"seen_post_ids": [], "active_alerts": {}}
    if not DATABASE_URL:
        data = _read_local(EWAY_LOCAL_FILE, default_state)
        return data if isinstance(data, dict) else default_state

    def work(cur):
        cur.execute("CREATE TABLE IF NOT EXISTS bot_data (key TEXT PRIMARY KEY, value JSONB NOT NULL)")
        cur.execute("SELECT value FROM bot_data WHERE key = %s", (EWAY_KEY,))
        row = cur.fetchone()
        if row is not None and isinstance(row[0], dict):
            return row[0]
        return _read_local(EWAY_LOCAL_FILE, default_state)

    try:
        return _run(work)
    except Exception as e:
        logging.error(f"Не вдалося завантажити стан EasyWay з бази: {e}")
        return _read_local(EWAY_LOCAL_FILE, default_state)


def save_eway_state(data: dict) -> None:
    payload = json.dumps(data, ensure_ascii=False, indent=2)

    def work():
        if DATABASE_URL:
            try:
                _run(lambda cur: _upsert(cur, payload, EWAY_KEY))
                return
            except Exception as e:
                logging.error(f"Не вдалося зберегти стан EasyWay в базу: {e}. Записую у локальний файл.")
        _write_local(payload, EWAY_LOCAL_FILE)

    _executor.submit(work)