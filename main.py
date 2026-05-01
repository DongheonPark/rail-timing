from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, FileResponse
from contextlib import asynccontextmanager
from datetime import datetime
import httpx
import json
import os
from dotenv import load_dotenv

load_dotenv()

SEOUL_API_KEY      = os.getenv("SEOUL_API_KEY")
SUBWAY_API_KEY     = os.getenv("SUBWAY_API_KEY")
CONGESTION_API_KEY = os.getenv("CONGESTION_API_KEY")
BOARDING_API_KEY   = os.getenv("BOARDING_API_KEY")

# TIME0530~TIME0030 컬럼 → 시:분 슬롯 매핑
TIME_COLS = {
    "TIME0530":"05:30","TIME0600":"06:00","TIME0630":"06:30","TIME0700":"07:00",
    "TIME0730":"07:30","TIME0800":"08:00","TIME0830":"08:30","TIME0900":"09:00",
    "TIME0930":"09:30","TIME1000":"10:00","TIME1030":"10:30","TIME1100":"11:00",
    "TIME1130":"11:30","TIME1200":"12:00","TIME1230":"12:30","TIME1300":"13:00",
    "TIME1330":"13:30","TIME1400":"14:00","TIME1430":"14:30","TIME1500":"15:00",
    "TIME1530":"15:30","TIME1600":"16:00","TIME1630":"16:30","TIME1700":"17:00",
    "TIME1730":"17:30","TIME1800":"18:00","TIME1830":"18:30","TIME1900":"19:00",
    "TIME1930":"19:30","TIME2000":"20:00","TIME2030":"20:30","TIME2100":"21:00",
    "TIME2130":"21:30","TIME2200":"22:00","TIME2230":"22:30","TIME2300":"23:00",
    "TIME2330":"23:30","TIME0000":"00:00","TIME0030":"00:30",
}
DOW_MAP = {"평일": "weekday", "토요일": "saturday", "일요일": "sunday"}

CONGESTION_DB: dict = {}  # {역명_호선: {weekday: {방향: {HH:MM: float}}}}
BOARDING_DB: dict = {}    # {역명_호선: {HH: {on: int, off: int}}}

# 실시간 도착 API 역명 → 혼잡도 API 역명 매핑
STATION_NAME_MAP = {
    "뚝섬유원지": "자양(뚝섬한강공원)",
    "신촌":       "신촌(지하)",
    "올림픽공원": "올림픽공원(한국체대)",
    "강동":       "강동(하남검단산)",
}


async def load_boarding():
    url = f"http://openapi.seoul.go.kr:8088/{BOARDING_API_KEY}/json/CardSubwayTime/1/621/202503/"
    async with httpx.AsyncClient() as client:
        r = await client.get(url, timeout=30)
        rows = r.json()["CardSubwayTime"]["row"]

    for row in rows:
        line = row["SBWY_ROUT_LN_NM"]
        station = row["STTN"]
        key = f"{station}_{line}"
        hourly = {}
        for h in range(4, 24):
            on  = int(row.get(f"HR_{h}_GET_ON_NOPE",  0) or 0)
            off = int(row.get(f"HR_{h}_GET_OFF_NOPE", 0) or 0)
            hourly[h] = {"on": on, "off": off}
        # 0~3시
        for h in range(0, 4):
            on  = int(row.get(f"HR_{h}_GET_ON_NOPE",  0) or 0)
            off = int(row.get(f"HR_{h}_GET_OFF_NOPE", 0) or 0)
            hourly[h] = {"on": on, "off": off}
        BOARDING_DB[key] = hourly


async def load_congestion():
    rows = []
    async with httpx.AsyncClient() as client:
        for start, end in [(1, 1000), (1001, 1671)]:
            url = f"http://openapi.seoul.go.kr:8088/{CONGESTION_API_KEY}/json/subwConfusion/{start}/{end}/"
            r = await client.get(url, timeout=30)
            rows += r.json()["subwConfusion"]["row"]

    for row in rows:
        key = f"{row['DPTRE_STTN']}_{row['LINE']}"
        if key not in CONGESTION_DB:
            CONGESTION_DB[key] = {"weekday": {}, "saturday": {}, "sunday": {}}
        day = DOW_MAP.get(row["DOW_SE"], "weekday")
        direction = row["UP_DOWN_SE"]
        CONGESTION_DB[key][day][direction] = {
            t: round(row.get(col, 0) or 0, 1) for col, t in TIME_COLS.items()
        }


@asynccontextmanager
async def lifespan(app: FastAPI):
    await load_congestion()
    await load_boarding()
    yield

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def serve_html():
    return FileResponse("rail-timing.html")


def korean_json(data) -> Response:
    return Response(
        content=json.dumps(data, ensure_ascii=False),
        media_type="application/json; charset=utf-8",
    )


def interpolate_congestion(slots: dict, target_minute: int) -> float:
    """target_minute(자정 기준 분)에서의 혼잡도를 인접 슬롯으로 선형 보간"""
    # 슬롯 목록을 분 단위로 변환
    slot_list = []
    for t, v in slots.items():
        h, m = int(t[:2]), int(t[3:])
        slot_list.append((h * 60 + m, v))
    slot_list.sort()

    if not slot_list:
        return 0.0

    # target보다 앞뒤 슬롯 찾기
    prev = next_ = None
    for sm, sv in slot_list:
        if sm <= target_minute:
            prev = (sm, sv)
        if sm > target_minute and next_ is None:
            next_ = (sm, sv)

    if prev is None:
        return slot_list[0][1]
    if next_ is None:
        return prev[1]

    span = next_[0] - prev[0]
    ratio = (target_minute - prev[0]) / span
    return round(prev[1] + (next_[1] - prev[1]) * ratio, 1)


@app.get("/api/status")
def root():
    return korean_json({"status": "ok", "message": "rail-timing API 서버 작동 중",
                        "congestion_stations": len(CONGESTION_DB)})


@app.get("/api/stations")
def get_stations(q: str = ""):
    names = sorted(set(key.rsplit("_", 1)[0] for key in CONGESTION_DB))
    if q:
        q_lower = q.lower()
        names = [n for n in names if q_lower in n]
    return korean_json(names)


@app.get("/api/congestion-all")
def get_congestion_all():
    """전체 혼잡도 슬롯 데이터 반환 (프론트 캐시용)
    형식: { "역명_호선": { "weekday": { "상선": {"HH:MM": float} } } }
    """
    return korean_json(CONGESTION_DB)


@app.get("/api/boarding-all")
def get_boarding_all():
    """전체 승하차 인원 반환 (프론트 캐시용)
    형식: { "역명_호선": { 4: {on: int, off: int}, 5: ..., ... } }
    """
    return korean_json(BOARDING_DB)


@app.get("/api/boarding/{station_name}/{line}")
def get_boarding(station_name: str, line: str, hour: int = -1):
    key = f"{station_name}_{line}"
    entry = BOARDING_DB.get(key)
    if not entry:
        return korean_json({"error": "데이터 없음", "key": key})
    if hour >= 0:
        return korean_json({"station": station_name, "line": line, "hour": hour, "data": entry.get(hour)})
    return korean_json({"station": station_name, "line": line, "data": entry})


@app.get("/api/arrivals/{station_name}")
async def get_arrivals(station_name: str):
    url = f"http://swopenapi.seoul.go.kr/api/subway/{SUBWAY_API_KEY}/json/realtimeStationArrival/1/30/{station_name}"
    async with httpx.AsyncClient() as client:
        response = await client.get(url, timeout=10.0)
        data = response.json()
    return korean_json(data)


@app.get("/api/congestion/{station_name}/{line}")
def get_congestion(station_name: str, line: str, barvlDt: int = 0,
                   simTime: str = None, dayType: str = None):
    now = datetime.now()

    if simTime:
        h, m = int(simTime[:2]), int(simTime[3:])
        base_min = h * 60 + m
    else:
        base_min = now.hour * 60 + now.minute

    day_key = dayType if dayType in ("weekday", "saturday", "sunday") else (
        "weekday" if now.weekday() < 5 else ("saturday" if now.weekday() == 5 else "sunday")
    )
    target_min = base_min + barvlDt // 60

    mapped = STATION_NAME_MAP.get(station_name, station_name)
    key = f"{mapped}_{line}"
    entry = CONGESTION_DB.get(key)
    if not entry:
        return korean_json({"error": "데이터 없음", "key": key})

    day_data = entry.get(day_key, {})
    directions = {d: interpolate_congestion(slots, target_min) for d, slots in day_data.items()}

    # 전체 슬롯 데이터 (차트용)
    slots_by_direction = {d: slots for d, slots in day_data.items()}

    arrival_time = f"{(target_min // 60) % 24:02d}:{target_min % 60:02d}"
    return korean_json({
        "station": station_name,
        "line": line,
        "day_type": day_key,
        "arrival_time": arrival_time,
        "data_source": "서울교통공사 혼잡도 통계 (보간 예측)",
        "directions": directions,
        "slots": slots_by_direction,
    })

