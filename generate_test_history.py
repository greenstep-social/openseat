"""
Генерирует синтетическую историю occupancy/шума за последние DAYS_BACK дней
и пишет её в ту же occupancy.db, что использует room_occupancy_server.py —
чтобы протестировать графики "типичный день", не дожидаясь месяца реальной
эксплуатации.

ТОЛЬКО ДЛЯ ТЕСТА. Не запускать на проде — данные добавятся вперемешку с
реальными (не удаляют существующее, но смешают тестовое с настоящим,
графики перестанут отражать реальность). Проще всего гонять на отдельной
копии occupancy.db или удалить файл после теста и дать серверу создать
базу заново.

Установка: только стандартная библиотека Python, ничего ставить не нужно
(cv2/ultralytics не требуются — это отдельный лёгкий скрипт).

Запуск:
    python generate_test_history.py
"""

import sqlite3
import random
from datetime import datetime, timedelta, timezone

DB_PATH = "occupancy.db"
DAYS_BACK = 30
INTERVAL_MINUTES = 5  # синтетическая частота — гуще реальной (5 сек), но
                       # этого достаточно для агрегата по часам и быстрее генерируется

# Комнаты для теста — подставь свои реальные room_id/вместимость, если хочешь
# протестировать конкретно на них
ROOMS = {
    "room_1": {"capacity": 6, "noise_floor": -48},
    "room_2": {"capacity": 8, "noise_floor": -44},
}


def init_schema(conn):
    """Та же схема, что создаёт room_occupancy_server.py — дублируем здесь,
    чтобы скрипт не тянул зависимости всего сервера (cv2, ultralytics)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS occupancy_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id TEXT NOT NULL,
            count INTEGER NOT NULL,
            timestamp TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_room_time ON occupancy_history (room_id, timestamp)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS noise_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id TEXT NOT NULL,
            db_level REAL NOT NULL,
            timestamp TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_noise_room_time ON noise_history (room_id, timestamp)")


def business_hours_factor(hour: int, is_weekend: bool) -> float:
    """0..1 — насколько "занят" этот час дня. Пик около 14:00, спад к
    вечеру/утру, почти пусто по выходным и ночью."""
    if is_weekend:
        return random.uniform(0.0, 0.08)
    if hour < 8 or hour > 20:
        return random.uniform(0.0, 0.05)
    peak_hour = 14
    base = max(0.05, 1 - abs(hour - peak_hour) / 9)
    return min(1.0, base + random.uniform(-0.1, 0.1))


def generate():
    conn = sqlite3.connect(DB_PATH)
    init_schema(conn)

    now = datetime.now().astimezone()  # локальное время системы, с таймзоной
    start = now - timedelta(days=DAYS_BACK)

    occupancy_rows = []
    noise_rows = []

    t = start
    step = timedelta(minutes=INTERVAL_MINUTES)
    while t <= now:
        hour = t.hour
        is_weekend = t.weekday() >= 5
        factor = business_hours_factor(hour, is_weekend)
        utc_ts = t.astimezone(timezone.utc).isoformat()

        for room_id, cfg in ROOMS.items():
            capacity = cfg["capacity"]
            count = round(capacity * factor + random.uniform(-0.5, 0.5))
            count = max(0, min(capacity, count))

            # шум растёт вместе с занятостью, плюс случайный фон
            noise_db = cfg["noise_floor"] + (count / capacity) * 18 + random.uniform(-3, 3)

            occupancy_rows.append((room_id, count, utc_ts))
            noise_rows.append((room_id, round(noise_db, 1), utc_ts))

        t += step

    conn.executemany(
        "INSERT INTO occupancy_history (room_id, count, timestamp) VALUES (?, ?, ?)",
        occupancy_rows,
    )
    conn.executemany(
        "INSERT INTO noise_history (room_id, db_level, timestamp) VALUES (?, ?, ?)",
        noise_rows,
    )
    conn.commit()
    conn.close()

    print(f"Сгенерировано {len(occupancy_rows)} occupancy-записей и {len(noise_rows)} noise-записей")
    print(f"Период: {start.date()} — {now.date()}, комнаты: {', '.join(ROOMS)}")
    print("Теперь можно проверить, например:")
    print("  http://localhost:8000/api/occupancy/room_1/daily_profile?days=30")
    print("  http://localhost:8000/api/noise/room_1/daily_profile?days=30&weekday_filter=weekday")


if __name__ == "__main__":
    generate()
