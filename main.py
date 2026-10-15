"""OpenSeat: ESP32 + FastAPI + Supabase. Python 3.10+."""
import json
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
import uvicorn
from urllib.request import Request, urlopen
from urllib.parse import urlencode, urlsplit, parse_qs
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parent
# Переменные окружения имеют приоритет над .env рядом с main.py.
if (ROOT / '.env').exists():
    for line in (ROOT / '.env').read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            name, value = line.split('=', 1)
            os.environ.setdefault(name.strip(), value.strip().strip('\"\''))
URL = os.getenv('SUPABASE_URL', 'https://mufzyszenjhdaanlcmyk.supabase.co').rstrip('/')
KEY = os.getenv('SUPABASE_KEY', '').strip()

class Space(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,79}$")
    name: str = Field(min_length=1, max_length=200)
    sensor_location: str = Field(min_length=1, max_length=200)
    description: str = ""
    photo: str = ""
    building: str = ""
    floor: int | None = Field(default=None, ge=0)
    capacity: int | None = Field(default=None, gt=0, strict=True)
    occupancy_enabled: bool = False

    @field_validator("photo")
    @classmethod
    def photo_path(cls, value):
        if value and not re.fullmatch(r"assets/[a-zA-Z0-9_./-]+", value):
            raise ValueError("photo: укажите локальное изображение в assets")
        if ".." in value.split("/"):
            raise ValueError("photo: путь не должен содержать ..")
        return value

    @field_validator("name", "sensor_location")
    @classmethod
    def nonblank(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Значение не должно быть пустым")
        return value

SPACES = [Space.model_validate(row) for row in json.loads((ROOT / "spaces.json").read_text(encoding="utf-8"))]
if len({p.id for p in SPACES}) != len(SPACES) or len({p.sensor_location for p in SPACES}) != len(SPACES):
    raise ValueError("spaces.json: id и sensor_location должны быть уникальны")
LOCATIONS = {p.sensor_location: p for p in SPACES}

class DatabaseError(Exception):
    pass

def query(params=None, payload=None, table="noise_data"):
    if table not in ("noise_data", "occupancy_data"):
        raise DatabaseError("Неизвестная таблица измерений.")
    if not KEY or KEY == 'PASTE_KEY_HERE':
        raise DatabaseError('Добавьте SUPABASE_KEY в файл .env и перезапустите сервер.')
    if urlsplit(URL).scheme != 'https' or not urlsplit(URL).netloc:
        raise DatabaseError('SUPABASE_URL должен быть HTTPS-адресом проекта Supabase.')
    headers = {'apikey': KEY, 'Accept': 'application/json'}
    if KEY.startswith('eyJ'):
        headers['Authorization'] = 'Bearer ' + KEY
    endpoint = URL + '/rest/v1/' + table
    if params:
        endpoint += '?' + urlencode(params)
    body = None
    if payload is not None:
        headers['Content-Type'] = 'application/json'
        headers['Prefer'] = 'return=minimal'
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
    req = Request(endpoint, headers=headers, data=body,
                  method='POST' if payload is not None else 'GET')
    try:
        with urlopen(req, timeout=12) as response:
            if payload is not None:
                return None
            result = json.load(response)
        if not isinstance(result, list):
            raise DatabaseError('Supabase вернул неожиданный формат ответа.')
        return result
    except HTTPError as exc:
        try:
            code = json.loads(exc.read()).get('code', '')
        except (ValueError, AttributeError):
            code = ''
        if exc.code in (401, 403):
            message = 'Supabase отклонил доступ. Проверьте ключ, права SELECT/INSERT и политики RLS.'
        elif code in ('42P01', 'PGRST205'):
            message = f'Таблица public.{table} не найдена или не доступна через Data API.'
        elif code == '42703':
            message = f'Проверьте столбцы таблицы {table}.'
        else:
            message = f'Ошибка Supabase: HTTP {exc.code}, код {code or "не указан"}. Проверьте Data API проекта.'
        raise DatabaseError(message) from None
    except (URLError, TimeoutError, OSError):
        raise DatabaseError('Нет соединения с Supabase. Проверьте адрес проекта и интернет.') from None
    except ValueError:
        raise DatabaseError('Supabase вернул некорректный JSON.') from None

def classify_noise(value):
    return "Тихо" if value <= 200 else "Средне" if value < 500 else "Громко"

def measurements(location='', limit=1):
    params = {'select': 'location,noise_level,created_at', 'order': 'created_at.desc.nullslast', 'limit': str(limit)}
    if location:
        params['location'] = 'eq.' + location
    rows = query(params)
    for row in rows:
        try:
            if row.get('noise_level') is None or isinstance(row['noise_level'], bool):
                raise ValueError()
            value = float(row['noise_level'])
            if not math.isfinite(value) or value < 0:
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise DatabaseError('В noise_data найдено некорректное значение noise_level.')
        row['noise_level'] = value
        row['noise_category'] = classify_noise(value)
    return rows

app = FastAPI(title="OpenSeat: Библиотека")
# Только папка с публичными изображениями и шрифтом, не папка сервера.
app.mount("/assets", StaticFiles(directory=ROOT / "assets"), name="assets")

@app.exception_handler(DatabaseError)
async def database_error(request, exc):
    return JSONResponse(status_code=503, content={"detail": str(exc)},
                        headers={"Cache-Control": "no-store"})

class NoiseData(BaseModel):
    location: str = Field(min_length=1, max_length=200)
    noise_level: int = Field(ge=0, strict=True)

    @field_validator("location")
    @classmethod
    def validate_location(cls, value):
        value = value.strip()
        if value not in LOCATIONS:
            raise ValueError("Добавьте этот location в spaces.json (sensor_location) и перезапустите сервер")
        return value

@app.post("/noise")
def add_noise(data: NoiseData):
    value = data.noise_level
    row = {"location": data.location, "noise_level": value,
           "noise_category": classify_noise(value)}
    # created_at заполняется существующим DEFAULT в Supabase, как в старом сервере.
    # Успех возвращаем только после подтверждения INSERT базой.
    query(payload=row)
    print(f"Сохранено: {data.location}, шум {value}, {row['noise_category']}", flush=True)
    return {"status": "saved", **row}


class OccupancyData(BaseModel):
    location: str = Field(min_length=1, max_length=200)
    occupied: int = Field(ge=0, strict=True)
    total: int = Field(gt=0, strict=True)

    @field_validator("location")
    @classmethod
    def validate_location(cls, value):
        value = value.strip()
        if value not in LOCATIONS or not LOCATIONS[value].occupancy_enabled:
            raise ValueError("Добавьте точку и включите occupancy_enabled в spaces.json")
        return value

@app.post("/occupancy")
def add_occupancy(data: OccupancyData):
    if data.occupied > data.total:
        raise HTTPException(422, "Количество занятых мест больше общего количества.")
    capacity = LOCATIONS[data.location].capacity
    if capacity is not None and capacity != data.total:
        raise HTTPException(422, "total должен совпадать с capacity точки.")
    row = data.model_dump()
    query(payload=row, table="occupancy_data")
    return {"status": "saved", **row}

def occupancy_measurement(location):
    rows = query({'select':'location,occupied,total,created_at', 'location':'eq.'+location,
                  'order':'created_at.desc.nullslast', 'limit':'1'}, table="occupancy_data")
    if not rows:
        return None
    row = rows[0]
    if (type(row.get('occupied')) is not int or type(row.get('total')) is not int
        or row['total'] <= 0 or not 0 <= row['occupied'] <= row['total']):
        raise DatabaseError("Некорректные данные о загруженности.")
    capacity = LOCATIONS[location].capacity
    if capacity is not None and row['total'] != capacity:
        raise DatabaseError("Общее число мест не совпадает с capacity точки.")
    return {**row, 'load': round(100*row['occupied']/row['total'], 1), 'free':row['total']-row['occupied']}

@app.get("/api/spaces")
def spaces_snapshot():
    # Для каждой точки получаем её последнюю запись отдельно. Частый датчик
    # одной точки не вытесняет показания остальных из общей выборки.
    result = [{**p.model_dump(), 'noise':None, 'occupancy':None, 'errors':{}} for p in SPACES]
    def read_one(index, kind):
        location = SPACES[index].sensor_location
        try:
            if kind == 'noise':
                records = measurements(location, 1)
                return index, kind, records[0] if records else None, None
            return index, kind, occupancy_measurement(location), None
        except DatabaseError as exc:
            return index, kind, None, str(exc)
    jobs = [(i,'noise') for i in range(len(SPACES))]
    jobs += [(i,'occupancy') for i,p in enumerate(SPACES) if p.occupancy_enabled]
    with ThreadPoolExecutor(max_workers=max(1,min(32,len(jobs)))) as pool:
        futures = [pool.submit(read_one,*job) for job in jobs]
        for future in futures:
            index,kind,value,error = future.result()
            result[index][kind] = value
            if error:
                result[index]['errors'][kind] = error
    return JSONResponse(result, headers={"Cache-Control":"no-store"})

@app.get("/api/health")
def health():
    return {"status": "ok", "service": "OpenSeat", "locations": list(LOCATIONS)}

@app.get("/api/history")
def history(location: str = Query(default="", max_length=200)):
    return JSONResponse(measurements(location.strip(), 50), headers={"Cache-Control": "no-store"})

@app.get("/api/current")
def current(location: str = Query(default="test_room", max_length=200)):
    rows = measurements(location.strip(), 1)
    if not rows:
        raise HTTPException(404, "Нет доступных измерений для этого помещения.")
    return JSONResponse(rows[0], headers={"Cache-Control": "no-store"})

def site_file(name, media_type):
    path = ROOT / name
    if not path.is_file():
        raise HTTPException(500, f"Файл {name} должен находиться рядом с main.py.")
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-cache"})

@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
@app.get("/space.html", include_in_schema=False)
def index():
    return site_file("index.html", "text/html")

@app.get("/app.js", include_in_schema=False)
@app.get("/script.js", include_in_schema=False)
def javascript():
    return site_file("app.js", "text/javascript")

@app.get("/demo.html", include_in_schema=False)
def demo_page():
    return site_file("demo.html", "text/html")

@app.get("/catalog.json", include_in_schema=False)
def catalog_metadata():
    return JSONResponse([p.model_dump() for p in SPACES], headers={"Cache-Control": "no-cache"})

@app.get("/style.css", include_in_schema=False)
@app.get("/styles.css", include_in_schema=False)
def stylesheet():
    return site_file("style.css", "text/css")

if __name__ == "__main__":
    print("Сайт на этом компьютере: http://127.0.0.1:8000", flush=True)
    print("ESP32: http://<IP компьютера в Wi-Fi>:8000/noise", flush=True)
    # Порт совпадает с существующим Arduino-скетчем.
    # HOST/PORT от прежней версии .env намеренно не используются.
    uvicorn.run(app, host="0.0.0.0", port=8000)
