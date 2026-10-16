"""
Минимальный пример: подсчёт людей в нескольких комнатах по RTSP-камерам.

Установка:
    pip install ultralytics opencv-python fastapi uvicorn

Запуск:
    python room_occupancy_server.py
"""

import os
import time
import sqlite3
import threading
import subprocess
from collections import deque, defaultdict
from datetime import datetime, timezone, timedelta

# Принудительно используем TCP вместо UDP для RTSP внутри OpenCV — на WiFi
# UDP теряет пакеты при нестабильном сигнале, что часто выглядит как
# "Stream timeout triggered" и зависания. Должно быть установлено ДО
# создания первого cv2.VideoCapture.
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"

import cv2
import numpy as np
from ultralytics import YOLO
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
import uvicorn

# ---------------------------------------------------------------------------
# 1. Конфигурация камер — заполни своими RTSP-адресами
# ---------------------------------------------------------------------------
CAMERAS = {
    "room_1": "rtsp://admin:Polk_232527*@10.8.116.250:554/Streaming/Channels/101",
    # "room_2": "rtsp://user:pass@192.168.1.102:554/stream1",  # раскомментируй, когда появится реальная камера
    # "room_3": "rtsp://user:pass@192.168.1.103:554/stream1",
    # "room_4": "rtsp://user:pass@192.168.1.104:554/stream1",
    # "room_5": "rtsp://user:pass@192.168.1.105:554/stream1",
}

CAPTURE_INTERVAL_SEC = 5      # как часто брать кадр
CONFIDENCE_THRESHOLD = 0.4    # порог уверенности детекции
PERSON_CLASS_ID = 0           # в COCO класс "person" имеет id 0
DB_PATH = "occupancy.db"

# ---------------------------------------------------------------------------
# 1b. Конфигурация звука — на каждую комнату нужен URL с ВКЛЮЧЁННЫМ аудио-каналом
#     (для Hikvision -IWS это тот же RTSP-поток, но аудио надо явно включить
#     в веб-интерфейсе камеры / через ONVIF, по умолчанию оно часто выключено)
# ---------------------------------------------------------------------------
NOISE_CAMERAS = {
    "room_1": "rtsp://admin:Polk_232527*@10.8.116.250:554/Streaming/Channels/101",
    # "room_2": "rtsp://user:pass@192.168.1.102:554/stream1",  # раскомментируй по мере добавления камер
    # "room_3": "rtsp://user:pass@192.168.1.103:554/stream1",
    # "room_4": "rtsp://user:pass@192.168.1.104:554/stream1",
    # "room_5": "rtsp://user:pass@192.168.1.105:554/stream1",
}

NOISE_MEASURE_INTERVAL_SEC = 5   # как часто снимать сырой замер
AUDIO_SAMPLE_RATE = 16000        # частота дискретизации, с которой просим ffmpeg отдать PCM
AUDIO_REFERENCE = 1.0            # опорное значение для относительной шкалы дБ (не абсолютный SPL!)

# Сглаживание: db_level, который видит сайт, — это усреднённая ПО ЭНЕРГИИ
# (не по дБ напрямую — так акустически корректно, как Leq у шумомеров)
# величина за последние NOISE_SMOOTHING_WINDOW_SEC секунд, а не мгновенный
# замер за 5 сек. Так один хлопок дверью не будет дёргать показания —
# отражается общий фон, а не случайный пик.
NOISE_SMOOTHING_WINDOW_SEC = 60
NOISE_WINDOW_SAMPLES = max(1, NOISE_SMOOTHING_WINDOW_SEC // NOISE_MEASURE_INTERVAL_SEC)

# Диапазон часов для графика "типичный день" — вне рабочих часов паттерн
# обычно не интересен (пусто/тихо), показываем только 8:00-22:00
DAILY_PROFILE_HOUR_START = 8
DAILY_PROFILE_HOUR_END = 22

# Пороги категорий шума — калибруются по месту (метрика относительная,
# зависит от конкретного микрофона и усиления камеры). Значения ниже —
# отправная точка, поправь после недели реальных замеров в своих комнатах.
NOISE_QUIET_MAX = -35   # db_level <= этого -> "тихо"
NOISE_MEDIUM_MAX = -20  # db_level <= этого -> "средне", выше -> "громко"

# ---------------------------------------------------------------------------
# 1c. Вместимость комнат — для расчёта процента загруженности и свободных мест
# ---------------------------------------------------------------------------
ROOM_CAPACITY = {
    "room_1": 6,
    "room_2": 8,
    "room_3": 12,
    "room_4": 4,
    "room_5": 10,
    # укажи реальную вместимость своих комнат
}

# ---------------------------------------------------------------------------
# 1a. SQLite — простое хранилище истории (для 5-10 комнат более чем достаточно)
# ---------------------------------------------------------------------------
db_lock = threading.Lock()  # sqlite3 с check_same_thread=False требует ручной синхронизации


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS occupancy_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id TEXT NOT NULL,
                count INTEGER NOT NULL,
                timestamp TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_room_time
            ON occupancy_history (room_id, timestamp)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS noise_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id TEXT NOT NULL,
                db_level REAL NOT NULL,
                timestamp TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_noise_room_time
            ON noise_history (room_id, timestamp)
        """)


def save_reading(room_id: str, count: int, timestamp: str):
    with db_lock:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO occupancy_history (room_id, count, timestamp) VALUES (?, ?, ?)",
                (room_id, count, timestamp),
            )


def fetch_history(room_id: str, since_iso: str):
    with db_lock:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                """SELECT count, timestamp FROM occupancy_history
                   WHERE room_id = ? AND timestamp >= ?
                   ORDER BY timestamp ASC""",
                (room_id, since_iso),
            ).fetchall()
    return [{"count": c, "timestamp": t} for c, t in rows]


def save_noise_reading(room_id: str, db_level: float, timestamp: str):
    with db_lock:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO noise_history (room_id, db_level, timestamp) VALUES (?, ?, ?)",
                (room_id, db_level, timestamp),
            )


def fetch_noise_history(room_id: str, since_iso: str):
    with db_lock:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                """SELECT db_level, timestamp FROM noise_history
                   WHERE room_id = ? AND timestamp >= ?
                   ORDER BY timestamp ASC""",
                (room_id, since_iso),
            ).fetchall()
    return [{"db_level": d, "timestamp": t} for d, t in rows]


def compute_daily_profile(readings, value_key, hour_start, hour_end, weekday_filter="all"):
    """Группирует показания по ЧАСУ ДНЯ ПО МЕСТНОМУ ВРЕМЕНИ сервера (не
    UTC — timestamp в БД хранится в UTC, конвертируем через .astimezone()),
    в диапазоне [hour_start, hour_end], опционально фильтруя будни/выходные.
    Возвращает среднее, 25-й/75-й перцентиль и min/max на каждый час —
    перцентили нужны, чтобы отличить "стабильно так" от "редкий выброс"."""
    buckets = defaultdict(list)
    for r in readings:
        value = r.get(value_key)
        if value is None:
            continue
        local_dt = datetime.fromisoformat(r["timestamp"]).astimezone()
        hour = local_dt.hour
        if hour < hour_start or hour > hour_end:
            continue
        is_weekend = local_dt.weekday() >= 5  # 5=суббота, 6=воскресенье
        if weekday_filter == "weekday" and is_weekend:
            continue
        if weekday_filter == "weekend" and not is_weekend:
            continue
        buckets[hour].append(value)

    profile = []
    for hour in range(hour_start, hour_end + 1):
        values = sorted(buckets.get(hour, []))
        n = len(values)
        if n == 0:
            profile.append({"hour": hour, "avg": None, "p25": None, "p75": None,
                             "min": None, "max": None, "samples": 0})
            continue
        profile.append({
            "hour": hour,
            "avg": round(sum(values) / n, 1),
            "p25": round(values[int(n * 0.25)], 1),
            "p75": round(values[min(int(n * 0.75), n - 1)], 1),
            "min": round(values[0], 1),
            "max": round(values[-1], 1),
            "samples": n,
        })
    return profile


# ---------------------------------------------------------------------------
# 2. Общее хранилище последних результатов (в памяти — для быстрого /api/occupancy)
# ---------------------------------------------------------------------------
latest_results = {
    room_id: {"count": 0, "updated_at": None, "status": "starting"}
    for room_id in CAMERAS
}
results_lock = threading.Lock()

latest_noise = {
    room_id: {"db_level": None, "db_level_instant": None, "updated_at": None, "status": "starting"}
    for room_id in NOISE_CAMERAS
}
noise_lock = threading.Lock()

# Скользящее окно последних NOISE_WINDOW_SAMPLES замеров МОЩНОСТИ (не дБ —
# усреднять нужно линейную энергию, дБ усреднять напрямую некорректно)
# на комнату, для расчёта сглаженного db_level.
noise_windows = {room_id: deque(maxlen=NOISE_WINDOW_SAMPLES) for room_id in NOISE_CAMERAS}

# Последний кадр с каждой камеры (JPEG-байты) — используется как "фото" комнаты
# на сайте вместо статичной стоковой картинки. Хранится только в памяти,
# перезаписывается каждым новым кадром.
latest_frames = {room_id: None for room_id in CAMERAS}
frames_lock = threading.Lock()

# То же самое, но с нарисованными рамками детекции — для отладки/калибровки
latest_debug_frames = {room_id: None for room_id in CAMERAS}
debug_frames_lock = threading.Lock()

# Самый свежий СЫРОЙ кадр с камеры (объект numpy, не JPEG) — обновляется
# непрерывно отдельным потоком-читателем, чтобы избежать буферизации RTSP
# внутри OpenCV (если читать редко, кадры копятся в буфере и отдаются
# с накопительной задержкой вместо самых свежих).
latest_raw_frames = {room_id: None for room_id in CAMERAS}
raw_frame_lock = threading.Lock()

# ---------------------------------------------------------------------------
# 3. Модель — одна на все потоки (ultralytics потокобезопасен для inference)
# ---------------------------------------------------------------------------
model = YOLO("yolov8n.pt")  # nano-версия: быстрая, ~6MB, скачается сама


def detect_people(frame):
    """Возвращает (count, results) — results нужен отдельно, чтобы потом
    нарисовать рамки для debug-эндпоинта, не гоняя модель второй раз."""
    results = model(frame, verbose=False)[0]
    count = 0
    for box in results.boxes:
        cls_id = int(box.cls[0])
        conf = float(box.conf[0])
        if cls_id == PERSON_CLASS_ID and conf >= CONFIDENCE_THRESHOLD:
            count += 1
    return count, results


def camera_reader_worker(room_id: str, rtsp_url: str):
    """Непрерывно вычитывает RTSP-поток в отдельном потоке и ничего не
    обрабатывает — только держит в памяти самый свежий кадр. Без этого
    OpenCV буферизует поток изнутри: если читать редко (например, раз в
    5 сек, как делает обработчик), read() отдаёт не текущий кадр, а один
    из накопившихся в очереди — отсюда накопительная задержка картинки.
    Быстрое непрерывное чтение "сливает" буфер, и обработчик всегда берёт
    действительно последний кадр."""
    cap = None

    while True:
        try:
            if cap is None or not cap.isOpened():
                cap = cv2.VideoCapture(rtsp_url)
                if not cap.isOpened():
                    raise RuntimeError("не удалось открыть поток")

            ok, frame = cap.read()
            if not ok:
                raise RuntimeError("не удалось прочитать кадр")

            with raw_frame_lock:
                latest_raw_frames[room_id] = frame

        except Exception:
            if cap is not None:
                cap.release()
                cap = None
            time.sleep(3)  # не долбим переподключением слишком часто при обрыве


def process_frame(room_id: str, frame):
    """Общая логика обработки одного кадра: YOLO-детекция, запись в БД,
    сохранение JPEG и debug-кадра с рамками. Используется и RTSP-воркером
    (camera_worker), и push-эндпоинтом для ESP32-CAM — источник кадра не
    важен, дальше обработка одинаковая."""
    count, results = detect_people(frame)
    now_iso = datetime.now(timezone.utc).isoformat()

    with results_lock:
        latest_results[room_id] = {
            "count": count,
            "updated_at": now_iso,
            "status": "ok",
        }

    save_reading(room_id, count, now_iso)

    # Сохраняем сам кадр как JPEG — покажем его на сайте вместо
    # стоковой картинки, это реальный (пусть и не совсем свежий) вид комнаты
    ok_encode, jpeg_bytes = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
    if ok_encode:
        with frames_lock:
            latest_frames[room_id] = jpeg_bytes.tobytes()

    # Debug-кадр: та же картинка, но с рамками — рисуем ТОЛЬКО те детекции,
    # что реально попали в count (класс person + наш CONFIDENCE_THRESHOLD),
    # а не все, что видит модель по своему внутреннему порогу
    person_mask = (results.boxes.cls == PERSON_CLASS_ID) & (results.boxes.conf >= CONFIDENCE_THRESHOLD)
    filtered_results = results[person_mask]
    debug_frame = filtered_results.plot()
    ok_debug, debug_jpeg = cv2.imencode(".jpg", debug_frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
    if ok_debug:
        with debug_frames_lock:
            latest_debug_frames[room_id] = debug_jpeg.tobytes()

    return count


def camera_worker(room_id: str, rtsp_url: str):
    """Отдельный поток на комнату: раз в CAPTURE_INTERVAL_SEC берёт самый
    свежий кадр (уже вычитанный camera_reader_worker) и прогоняет через
    YOLO. Сам к RTSP не подключается — этим занимается reader-поток."""

    while True:
        with raw_frame_lock:
            frame = latest_raw_frames.get(room_id)

        if frame is None:
            # Reader ещё не успел прочитать ни одного кадра (например,
            # сервер только что стартовал) или поток недоступен
            with results_lock:
                latest_results[room_id]["status"] = "starting"
            time.sleep(CAPTURE_INTERVAL_SEC)
            continue

        try:
            process_frame(room_id, frame)
        except Exception as e:
            with results_lock:
                latest_results[room_id]["status"] = f"error: {e}"

        time.sleep(CAPTURE_INTERVAL_SEC)


# ---------------------------------------------------------------------------
# 3a. Оценка уровня шума — ffmpeg отдаёт сырой PCM, мы считаем RMS/дБ
#     и НИКОГДА не сохраняем и не логируем сами аудио-сэмплы, только число.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 3a. Оценка уровня шума — ffmpeg отдаёт сырой PCM, мы считаем RMS/дБ
#     и НИКОГДА не сохраняем и не логируем сами аудио-сэмплы, только число.
# ---------------------------------------------------------------------------
def mean_square_power(samples: np.ndarray) -> float:
    """Средняя МОЩНОСТЬ блока PCM-сэмплов (int16, нормализованных в [-1, 1]).
    Именно эту величину нужно усреднять по времени (не дБ напрямую — дБ
    логарифмическая шкала, арифметическое усреднение по ней даёт
    акустически неверный результат)."""
    return float(np.mean(samples.astype(np.float64) ** 2)) + 1e-12


def power_to_relative_db(power: float) -> float:
    """Переводит мощность в относительный уровень громкости в дБ. Это НЕ
    откалиброванный SPL — только относительная метрика для сравнения
    "тише/громче" в рамках одной и той же камеры/усиления."""
    return 10 * np.log10(power / (AUDIO_REFERENCE ** 2))


def noise_category(db_level):
    """Переводит db_level в одну из трёх категорий для отображения на сайте."""
    if db_level is None:
        return None
    if db_level <= NOISE_QUIET_MAX:
        return "quiet"
    if db_level <= NOISE_MEDIUM_MAX:
        return "medium"
    return "loud"


def noise_worker(room_id: str, rtsp_url: str):
    """Отдельный поток на комнату: тянет аудио-дорожку RTSP через ffmpeg,
    раз в NOISE_MEASURE_INTERVAL_SEC считает мгновенный уровень и обновляет
    скользящее среднее по энергии за последние NOISE_SMOOTHING_WINDOW_SEC —
    именно оно идёт в db_level и на сайт, мгновенное значение доступно
    отдельно как db_level_instant. Сырые сэмплы существуют только в
    оперативной памяти этой функции и никогда никуда не пишутся и не логируются."""
    bytes_per_sample = 2  # s16le = 2 байта на сэмпл
    chunk_bytes = AUDIO_SAMPLE_RATE * bytes_per_sample * NOISE_MEASURE_INTERVAL_SEC

    while True:
        proc = None
        try:
            # -vn: игнорируем видео, забираем только аудио-дорожку потока
            proc = subprocess.Popen(
                [
                    "ffmpeg", "-i", rtsp_url,
                    "-vn",
                    "-acodec", "pcm_s16le",
                    "-ar", str(AUDIO_SAMPLE_RATE),
                    "-ac", "1",
                    "-f", "s16le",
                    "-loglevel", "error",
                    "pipe:1",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,  # захватываем, чтобы показать реальную причину при сбое
            )

            while True:
                raw = proc.stdout.read(chunk_bytes)
                if not raw or len(raw) < chunk_bytes // 2:
                    # процесс уже умер/умирает — читаем, что ffmpeg написал в stderr,
                    # чтобы в status была настоящая причина, а не общая фраза
                    stderr_tail = ""
                    try:
                        proc.kill()
                        stderr_bytes = proc.stderr.read(2000) if proc.stderr else b""
                        stderr_tail = stderr_bytes.decode(errors="replace").strip()[-300:]
                    except Exception:
                        pass
                    detail = f" | ffmpeg: {stderr_tail}" if stderr_tail else " (аудио включено на камере?)"
                    raise RuntimeError(f"аудио-поток прервался или пуст{detail}")

                samples = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
                instant_power = mean_square_power(samples)
                instant_db = round(float(power_to_relative_db(instant_power)), 1)
                # raw и samples перезапишутся на следующей итерации — не сохраняются

                # Сглаженное значение — среднее ПО МОЩНОСТИ за последние
                # NOISE_SMOOTHING_WINDOW_SEC секунд (Leq-подобный подход),
                # а не мгновенный замер за эти 5 сек
                noise_windows[room_id].append(instant_power)
                smoothed_power = sum(noise_windows[room_id]) / len(noise_windows[room_id])
                smoothed_db = round(float(power_to_relative_db(smoothed_power)), 1)

                now_iso = datetime.now(timezone.utc).isoformat()
                with noise_lock:
                    latest_noise[room_id] = {
                        "db_level": smoothed_db,           # то, что видит сайт — сглаженное
                        "db_level_instant": instant_db,     # мгновенное — для отладки/калибровки
                        "updated_at": now_iso,
                        "status": "ok",
                    }
                save_noise_reading(room_id, smoothed_db, now_iso)

        except Exception as e:
            with noise_lock:
                latest_noise[room_id]["status"] = f"error: {e}"
            if proc is not None:
                proc.kill()
            time.sleep(NOISE_MEASURE_INTERVAL_SEC)



def start_workers():
    for room_id, url in CAMERAS.items():
        reader = threading.Thread(target=camera_reader_worker, args=(room_id, url), daemon=True)
        reader.start()

        processor = threading.Thread(target=camera_worker, args=(room_id, url), daemon=True)
        processor.start()

    for room_id, url in NOISE_CAMERAS.items():
        t = threading.Thread(target=noise_worker, args=(room_id, url), daemon=True)
        t.start()


# ---------------------------------------------------------------------------
# 5. FastAPI — отдаёт агрегированные данные на сайт
# ---------------------------------------------------------------------------
app = FastAPI(title="Room Occupancy API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # в продакшене сузить до домена сайта
    allow_methods=["GET"],
)


@app.get("/api/occupancy")
def get_occupancy():
    with results_lock:
        return {"rooms": latest_results, "server_time": datetime.now(timezone.utc).isoformat()}


@app.get("/api/occupancy/{room_id}")
def get_room_occupancy(room_id: str):
    with results_lock:
        return latest_results.get(room_id, {"error": "unknown room"})


@app.get("/api/occupancy/{room_id}/history")
def get_room_history(room_id: str, hours: float = 24):
    """История замеров occupancy за последние N часов (по умолчанию сутки)."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    return {"room_id": room_id, "since": since, "readings": fetch_history(room_id, since)}


@app.get("/api/occupancy/{room_id}/daily_profile")
def get_occupancy_daily_profile(room_id: str, days: float = 30, weekday_filter: str = "all"):
    """"Типичный день" по occupancy: среднее число людей по каждому часу
    (8:00-22:00) за последние `days` дней. weekday_filter: all/weekday/weekend.
    Это НЕ замена /history — /history остаётся для детального просмотра
    последних 24ч/7д, этот эндпоинт — для долгосрочного паттерна."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    readings = fetch_history(room_id, since)
    profile = compute_daily_profile(readings, "count", DAILY_PROFILE_HOUR_START, DAILY_PROFILE_HOUR_END, weekday_filter)
    return {
        "room_id": room_id, "days": days, "weekday_filter": weekday_filter,
        "hour_start": DAILY_PROFILE_HOUR_START, "hour_end": DAILY_PROFILE_HOUR_END,
        "profile": profile,
    }


@app.get("/api/noise")
def get_noise():
    with noise_lock:
        return {"rooms": latest_noise, "server_time": datetime.now(timezone.utc).isoformat()}


@app.get("/api/noise/{room_id}")
def get_room_noise(room_id: str):
    with noise_lock:
        return latest_noise.get(room_id, {"error": "unknown room"})


@app.get("/api/noise/{room_id}/history")
def get_room_noise_history(room_id: str, hours: float = 24):
    """История уровня шума за последние N часов. db_level — относительная
    метрика (не откалиброванный SPL), полезна для сравнения "тише/громче"."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    return {"room_id": room_id, "since": since, "readings": fetch_noise_history(room_id, since)}


@app.get("/api/noise/{room_id}/daily_profile")
def get_noise_daily_profile(room_id: str, days: float = 30, weekday_filter: str = "all"):
    """"Типичный день" по шуму: средний db_level по каждому часу (8:00-22:00)
    за последние `days` дней. weekday_filter: all/weekday/weekend."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    readings = fetch_noise_history(room_id, since)
    profile = compute_daily_profile(readings, "db_level", DAILY_PROFILE_HOUR_START, DAILY_PROFILE_HOUR_END, weekday_filter)
    return {
        "room_id": room_id, "days": days, "weekday_filter": weekday_filter,
        "hour_start": DAILY_PROFILE_HOUR_START, "hour_end": DAILY_PROFILE_HOUR_END,
        "profile": profile,
    }


@app.get("/api/frame/{room_id}")
def get_room_frame(room_id: str):
    """Последний кадр с камеры комнаты (JPEG) — для фото в карточке на сайте."""
    with frames_lock:
        jpeg_bytes = latest_frames.get(room_id)
    if jpeg_bytes is None:
        return Response(status_code=404, content=b"no frame yet")
    return Response(content=jpeg_bytes, media_type="image/jpeg")


@app.get("/api/frame/{room_id}/debug")
def get_room_frame_debug(room_id: str):
    """Тот же кадр, но с нарисованными рамками вокруг обнаруженных людей —
    открой в браузере, чтобы проверить, кого модель посчитала, а кого нет.
    Показывает ровно то, что попало в count (тот же CONFIDENCE_THRESHOLD)."""
    with debug_frames_lock:
        jpeg_bytes = latest_debug_frames.get(room_id)
    if jpeg_bytes is None:
        return Response(status_code=404, content=b"no debug frame yet")
    return Response(content=jpeg_bytes, media_type="image/jpeg")


@app.post("/api/frame/{room_id}/upload")
async def upload_room_frame(room_id: str, request: Request):
    """Push-приём кадра от устройств без RTSP (например, ESP32-CAM):
    устройство само шлёт JPEG раз в CAPTURE_INTERVAL_SEC POST-запросом с
    картинкой в теле запроса (Content-Type: image/jpeg). Не требует записи
    в CAMERAS — комната появится в /api/rooms сама после первого кадра."""
    body = await request.body()
    if not body:
        return Response(status_code=400, content=b"empty body")

    np_bytes = np.frombuffer(body, dtype=np.uint8)
    frame = cv2.imdecode(np_bytes, cv2.IMREAD_COLOR)
    if frame is None:
        return Response(status_code=400, content=b"could not decode JPEG")

    try:
        count = process_frame(room_id, frame)
    except Exception as e:
        with results_lock:
            latest_results.setdefault(room_id, {})["status"] = f"error: {e}"
        return Response(status_code=500, content=str(e).encode())

    return {"status": "ok", "count": count}


@app.get("/api/rooms")
def get_rooms_combined():
    """Единый эндпоинт для карточки комнаты на сайте: количество людей,
    процент загруженности, свободные места, уровень и категория шума."""
    with results_lock:
        occ_snapshot = {k: dict(v) for k, v in latest_results.items()}
    with noise_lock:
        noise_snapshot = {k: dict(v) for k, v in latest_noise.items()}

    room_ids = set(occ_snapshot) | set(noise_snapshot) | set(ROOM_CAPACITY)
    rooms = {}
    for room_id in room_ids:
        occ = occ_snapshot.get(room_id, {})
        noise = noise_snapshot.get(room_id, {})
        capacity = ROOM_CAPACITY.get(room_id)
        count = occ.get("count")

        percent = None
        free_spots = None
        if capacity and count is not None:
            percent = round(min(count, capacity) / capacity * 100)
            free_spots = max(capacity - count, 0)

        rooms[room_id] = {
            "count": count,
            "capacity": capacity,
            "occupancy_percent": percent,
            "free_spots": free_spots,
            "occupancy_status": occ.get("status"),
            "noise_db": noise.get("db_level"),
            "noise_category": noise_category(noise.get("db_level")),
            "noise_status": noise.get("status"),
            "updated_at": occ.get("updated_at") or noise.get("updated_at"),
            "photo_url": f"/api/frame/{room_id}",
        }

    return {"rooms": rooms, "server_time": datetime.now(timezone.utc).isoformat()}


if __name__ == "__main__":
    init_db()
    start_workers()
    uvicorn.run(app, host="0.0.0.0", port=8000)
