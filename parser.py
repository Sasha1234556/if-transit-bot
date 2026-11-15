import json
import re
import requests
from bs4 import BeautifulSoup

ROUTES_URLS = {
    "2": [
        "https://if.rozklad.in.ua/home/schedule/179",
        "https://if.rozklad.in.ua/home/schedule/180",
    ],
    "3": [
        "https://if.rozklad.in.ua/home/schedule/12",
        "https://if.rozklad.in.ua/home/schedule/11",
    ],
    "5": [
        "https://if.rozklad.in.ua/home/schedule/152",
        "https://if.rozklad.in.ua/home/schedule/153",
    ],
    "6": [
        "https://if.rozklad.in.ua/home/schedule/9",
        "https://if.rozklad.in.ua/home/schedule/15",
    ],
    "7": [
        "https://if.rozklad.in.ua/home/schedule/7",
        "https://if.rozklad.in.ua/home/schedule/8",
    ],
    "9": [
        "https://if.rozklad.in.ua/home/schedule/176",
        "https://if.rozklad.in.ua/home/schedule/177",
    ],
    "10": [
        "https://if.rozklad.in.ua/home/schedule/17",
        "https://if.rozklad.in.ua/home/schedule/18",
    ],
}

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def parse_schedule_table(table):
    """Витягує список відправлень у форматі 'HH:MM' з HTML-таблиці."""
    departures = []
    if not table:
        return departures
    for row in table.find_all("tr"):
        cells = [c.get_text(strip=True) for c in row.find_all(["th", "td"])]
        if not cells or not cells[0].isdigit():
            continue
        hour = int(cells[0])
        for m in cells[1:]:
            # Витягуємо тільки цифри хвилин (ігноруючи примітки на кшталт '15д')
            match = re.match(r"^(\d{1,2})", m)
            if match:
                minute = int(match.group(1))
                departures.append(f"{hour:02d}:{minute:02d}")
    return sorted(list(set(departures)))


def parse_page(url):
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "html.parser")

    # Назва напрямку (зазвичай у заголовку h1 або h2)
    title_tag = soup.find(["h1", "h2"])
    direction_name = title_tag.get_text(strip=True) if title_tag else url

    # Парсинг зупинок і хвилин зміщення (напр. "5'. вул. І.Миколайчука")
    stops = []
    text_blocks = soup.get_text("\n").split("\n")
    for line in text_blocks:
        line = line.strip()
        match = re.match(r"^(\d+)['′]\.\s*(.+)$", line)
        if match:
            offset = int(match.group(1))
            stop_name = match.group(2).strip()
            stops.append({"name": stop_name, "offset": offset})

    # Якщо перша зупинка не має 0', додаємо її з назви напрямку
    if stops and stops[0]["offset"] > 0:
        first_stop = direction_name.split("-")[0].strip()
        # Прибираємо "Тролейбус №X" з початку, якщо є
        first_stop = re.sub(r"^Тролейбус\s*№?\d+\s*", "", first_stop).strip()
        stops.insert(0, {"name": first_stop, "offset": 0})

    # Парсинг таблиць (перша зазвичай робочі дні, друга - вихідні)
    tables = soup.find_all("table")
    workdays = parse_schedule_table(tables[0]) if len(tables) >= 1 else []
    weekends = parse_schedule_table(tables[1]) if len(tables) >= 2 else workdays

    return {
        "url": url,
        "direction": direction_name,
        "stops": stops,
        "workdays": workdays,
        "weekends": weekends,
    }


def main():
    database = {}
    for route_num, urls in ROUTES_URLS.items():
        print(f"Збираю дані для тролейбуса №{route_num}...")
        database[route_num] = []
        for idx, url in enumerate(urls):
            data = parse_page(url)
            data["dir_id"] = str(idx)
            database[route_num].append(data)

    with open("schedules.json", "w", encoding="utf-8") as f:
        json.dump(database, f, ensure_ascii=False, indent=2)
    print("Готово! Дані збережено у schedules.json")


if __name__ == "__main__":
    main()