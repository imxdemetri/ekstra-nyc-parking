"""Ingest NYC DOT cameras, parking signs, and meters into PostGIS."""

import re
import httpx
from pyproj import Transformer
from psycopg2.extras import execute_values
from app.db import get_conn

DOT_CAMERAS_URL = "https://webcams.nyctmc.org/api/cameras"
SIGNS_URL = "https://data.cityofnewyork.us/resource/nfid-uabd.json"
METERS_URL = "https://data.cityofnewyork.us/resource/693u-uax6.json"

_transformer = Transformer.from_crs("EPSG:2263", "EPSG:4326", always_xy=True)


def _nys_to_latlon(x: float, y: float) -> tuple[float, float]:
    lng, lat = _transformer.transform(x, y)
    return lat, lng


def _parse_camera_name(name: str) -> tuple[str, str, str]:
    if "@" in name:
        parts = name.split("@", 1)
        return parts[0].strip(), parts[1].strip(), "intersection"
    return name, "", "highway"


# ── Street name normalization ─────────────────────────────────────────
# Camera says "1 Ave" or "E. Houston St"
# Signs say  "1 AVENUE" or "EAST HOUSTON STREET"
# We normalize both to a canonical form for matching

def _normalize_street(name: str) -> str:
    """Normalize a street name for matching. Returns uppercase canonical form."""
    s = name.upper().strip()
    # Remove extra whitespace
    s = re.sub(r'\s+', ' ', s)
    # Expand abbreviations
    s = re.sub(r'\bAVE\b\.?', 'AVENUE', s)
    s = re.sub(r'\bST\b\.?', 'STREET', s)
    s = re.sub(r'\bBLVD\b\.?', 'BOULEVARD', s)
    s = re.sub(r'\bDR\b\.?', 'DRIVE', s)
    s = re.sub(r'\bPKWY\b\.?', 'PARKWAY', s)
    s = re.sub(r'\bPL\b\.?', 'PLACE', s)
    s = re.sub(r'\bRD\b\.?', 'ROAD', s)
    s = re.sub(r'\bE\b\.?', 'EAST', s)
    s = re.sub(r'\bW\b\.?', 'WEST', s)
    s = re.sub(r'\bN\b\.?', 'NORTH', s)
    s = re.sub(r'\bS\b\.?', 'SOUTH', s)
    # Remove ordinal suffixes: "42ND" -> "42", "1ST" -> "1", "3RD" -> "3", "5TH" -> "5"
    s = re.sub(r'(\d+)\s*(ST|ND|RD|TH)\b', r'\1', s)
    return s.strip()


def _extract_street_number(name: str) -> str | None:
    """Extract the numeric part of a street name. '45 St' -> '45', 'Broadway' -> None."""
    m = re.search(r'\b(\d+)\b', name)
    return m.group(1) if m else None


def _build_cross_street_sql_pattern(cross_street: str) -> str:
    """Build a SQL LIKE pattern that matches the cross street in sign data.

    Camera: '45 St' -> needs to match 'WEST   45 STREET', 'EAST   45 STREET'
    Camera: 'Houston St' -> needs to match 'HOUSTON STREET', 'EAST HOUSTON STREET'
    """
    normalized = _normalize_street(cross_street)
    num = _extract_street_number(cross_street)

    if num:
        # Numeric street: match the number with word boundary
        # "WEST   45 STREET" — the number follows spaces
        # Use: LIKE '% 45 STREET%' OR LIKE '% 45 ST%' (handles both formats)
        return f"% {num} %"
    else:
        # Named street: use normalized name
        # "HOUSTON STREET" matches directly
        # Strip directional prefix for broader matching
        name_part = re.sub(r'^(EAST|WEST|NORTH|SOUTH)\s+', '', normalized)
        return f"%{name_part}%"


def _build_main_street_sql_pattern(main_street: str) -> str:
    """Build SQL LIKE pattern for the main street (the avenue/road the camera is on)."""
    normalized = _normalize_street(main_street)
    num = _extract_street_number(main_street)

    if num and ("AVENUE" in normalized or "AVE" in main_street.upper()):
        # "1 Ave" -> match "1 AVENUE"
        return f"{num} AVENUE"
    elif num:
        return f"% {num} %"
    else:
        # Named street: strip direction, use core name
        name_part = re.sub(r'^(EAST|WEST|NORTH|SOUTH)\s+', '', normalized)
        # Remove trailing AVENUE/STREET for broader match
        name_part = re.sub(r'\s+(AVENUE|STREET|BOULEVARD|DRIVE|ROAD|PLACE|PARKWAY)$', '', name_part)
        return f"%{name_part}%"


# ── Sign description parser ──────────────────────────────────────────

_DAY_NAMES = {
    "MONDAY": "monday", "TUESDAY": "tuesday", "WEDNESDAY": "wednesday",
    "THURSDAY": "thursday", "FRIDAY": "friday", "SATURDAY": "saturday", "SUNDAY": "sunday",
    "MON": "monday", "TUE": "tuesday", "TUES": "tuesday", "WED": "wednesday",
    "THU": "thursday", "THUR": "thursday", "FRI": "friday", "SAT": "saturday", "SUN": "sunday",
}

_TIME_RE = re.compile(r"(\d{1,2}(?::\d{2})?)\s*(AM|PM)", re.IGNORECASE)
_HOUR_RE = re.compile(r"(\d+)\s*H(?:R|MP)", re.IGNORECASE)


def _parse_time(s: str) -> str | None:
    m = _TIME_RE.search(s)
    if not m:
        return None
    raw, ampm = m.group(1), m.group(2).upper()
    if ":" in raw:
        h, mn = raw.split(":")
    else:
        h, mn = raw, "00"
    h = int(h)
    if ampm == "PM" and h != 12:
        h += 12
    if ampm == "AM" and h == 12:
        h = 0
    return f"{h:02d}:{mn}"


def _parse_days(desc: str) -> list[str]:
    upper = desc.upper()
    if "ANYTIME" in upper or "EVERYDAY" in upper:
        return ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    if "EXCEPT SUNDAY" in upper:
        return ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]
    if "EXCEPT SATURDAY" in upper:
        return ["monday", "tuesday", "wednesday", "thursday", "friday", "sunday"]
    if "MONDAY-FRIDAY" in upper or "MON-FRI" in upper:
        return ["monday", "tuesday", "wednesday", "thursday", "friday"]
    if "MONDAY-SATURDAY" in upper or "MON-SAT" in upper:
        return ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]
    found = []
    for token in re.split(r"[\s,/&]+", upper):
        token = token.strip(".-")
        if token in _DAY_NAMES:
            found.append(_DAY_NAMES[token])
    return found if found else ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _parse_sign_rule(desc: str) -> dict:
    upper = desc.upper()
    is_asp = False
    if "NO STANDING" in upper:
        rule_type = "no_standing"
    elif "NO STOPPING" in upper:
        rule_type = "no_standing"
    elif "NO PARKING" in upper and "SANITATION" in upper:
        rule_type = "no_parking"
        is_asp = True
    elif "NO PARKING" in upper:
        rule_type = "no_parking"
    elif "HMP" in upper or "METER" in upper or "PAY-BY-CELL" in upper:
        rule_type = "metered"
    elif "LOADING" in upper:
        rule_type = "loading"
    elif "BUS STOP" in upper or "BUS ONLY" in upper:
        rule_type = "bus_stop"
    elif "TAXI" in upper:
        rule_type = "taxi_stand"
    elif "AUTHORIZED" in upper or "PERMIT" in upper or "LICENSE PLATES ONLY" in upper:
        rule_type = "special_permit"
    else:
        rule_type = "other"

    times = _TIME_RE.findall(desc)
    start_time = None
    end_time = None
    if len(times) >= 2:
        start_time = _parse_time(f"{times[0][0]} {times[0][1]}")
        end_time = _parse_time(f"{times[1][0]} {times[1][1]}")
    elif len(times) == 1:
        start_time = _parse_time(f"{times[0][0]} {times[0][1]}")

    max_hours = None
    hm = _HOUR_RE.search(desc)
    if hm:
        max_hours = float(hm.group(1))

    days = _parse_days(desc)

    vehicle_restriction = None
    if "COMMERCIAL" in upper:
        vehicle_restriction = "commercial"
    elif "TRUCK" in upper:
        vehicle_restriction = "truck"
    elif "DOCTOR" in upper:
        vehicle_restriction = "doctor"
    elif "AUTHORIZED" in upper:
        vehicle_restriction = "authorized"

    return {
        "rule_type": rule_type, "days": days, "start_time": start_time,
        "end_time": end_time, "max_hours": max_hours,
        "vehicle_restriction": vehicle_restriction, "is_asp": is_asp,
    }


def _parse_meter_hours(hours_str: str) -> dict:
    if not hours_str:
        return {}
    upper = hours_str.upper()
    max_hours = None
    hm = re.search(r"(\d+)HR", upper)
    if hm:
        max_hours = float(hm.group(1))
    vehicle_type = "passenger"
    if "COM" in upper:
        vehicle_type = "commercial"
    days = _parse_days(upper)
    time_match = re.search(r"(\d{4})-(\d{4})", upper)
    start_time = None
    end_time = None
    if time_match:
        s, e = time_match.group(1), time_match.group(2)
        start_time = f"{s[:2]}:{s[2:]}"
        end_time = f"{e[:2]}:{e[2:]}"
    return {"max_hours": max_hours, "vehicle_type": vehicle_type, "days": days,
            "start_time": start_time, "end_time": end_time}


# ── Ingest functions ─────────────────────────────────────────────────

def ingest_cameras():
    print("[ingest] Fetching DOT cameras...")
    resp = httpx.get(DOT_CAMERAS_URL, timeout=30)
    cameras = resp.json()
    print(f"[ingest] Got {len(cameras)} cameras")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            rows = []
            for cam in cameras:
                main_st, cross_st, cam_type = _parse_camera_name(cam["name"])
                rows.append((
                    cam["id"], cam["name"], cam["latitude"], cam["longitude"],
                    cam.get("area"), cam.get("isOnline") == "true",
                    main_st, cross_st, cam_type, cam.get("imageUrl"),
                ))
            execute_values(cur, """
                INSERT INTO dot_cameras (id, name, latitude, longitude, area, is_online, main_street, cross_street, camera_type, image_url)
                VALUES %s
                ON CONFLICT (id) DO UPDATE SET is_online = EXCLUDED.is_online, last_synced_at = NOW()
            """, rows)
        conn.commit()
        print(f"[ingest] Upserted {len(cameras)} cameras")
    finally:
        conn.close()


def ingest_signs():
    print("[ingest] Fetching parking signs...")
    conn = get_conn()
    total = 0
    batch_size = 50000
    try:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE parking_signs RESTART IDENTITY")
            conn.commit()
            offset = 0
            while True:
                url = f"{SIGNS_URL}?$limit={batch_size}&$offset={offset}&$order=:id"
                print(f"[ingest] Signs batch: offset={offset} ...")
                resp = httpx.get(url, timeout=120)
                signs = resp.json()
                if not signs or not isinstance(signs, list):
                    break
                rows = []
                for s in signs:
                    lat, lng = None, None
                    x = s.get("sign_x_coord")
                    y = s.get("sign_y_coord")
                    if x and y:
                        try:
                            lat, lng = _nys_to_latlon(float(x), float(y))
                        except Exception:
                            pass
                    rule = _parse_sign_rule(s.get("sign_description", ""))
                    dist = None
                    d_raw = s.get("distance_from_intersection", "")
                    if d_raw and str(d_raw).strip():
                        try:
                            dist = int(float(d_raw))
                        except (ValueError, TypeError):
                            pass
                    rows.append((
                        s.get("order_number"), s.get("borough"),
                        s.get("on_street", ""), s.get("from_street", ""), s.get("to_street", ""),
                        s.get("side_of_street"), s.get("sign_code"), s.get("sign_description", ""),
                        dist, s.get("arrow_direction"),
                        float(x) if x else None, float(y) if y else None, lat, lng,
                        rule["rule_type"], rule["days"] or None,
                        rule.get("start_time"), rule.get("end_time"),
                        rule.get("max_hours"), rule.get("vehicle_restriction"), rule.get("is_asp", False),
                    ))
                execute_values(cur, """
                    INSERT INTO parking_signs (
                        order_number, borough, on_street, from_street, to_street,
                        side_of_street, sign_code, sign_description,
                        distance_from_intersection, arrow_direction,
                        sign_x, sign_y, latitude, longitude,
                        rule_type, days, start_time, end_time, max_hours,
                        vehicle_restriction, is_asp
                    ) VALUES %s
                """, rows, page_size=5000)
                conn.commit()
                total += len(signs)
                print(f"[ingest] Signs: {total} ingested")
                if len(signs) < batch_size:
                    break
                offset += batch_size
        print(f"[ingest] Total signs: {total}")
    finally:
        conn.close()


def ingest_meters():
    print("[ingest] Fetching parking meters...")
    conn = get_conn()
    total = 0
    try:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE parking_meters RESTART IDENTITY")
            conn.commit()
            offset = 0
            batch_size = 50000
            while True:
                url = f"{METERS_URL}?$limit={batch_size}&$offset={offset}"
                resp = httpx.get(url, timeout=60)
                meters = resp.json()
                if not meters or not isinstance(meters, list):
                    break
                rows = []
                for m in meters:
                    parsed = _parse_meter_hours(m.get("meter_hours", ""))
                    lat = float(m["lat"]) if m.get("lat") else None
                    lng = float(m["long"]) if m.get("long") else None
                    rows.append((
                        m.get("meter_number"), m.get("status"), m.get("pay_by_cell_number"),
                        m.get("meter_hours"), m.get("borough"), m.get("on_street"),
                        m.get("side_of_street"), m.get("from_street"), m.get("to_street"),
                        lat, lng, parsed.get("max_hours"), parsed.get("days"),
                        parsed.get("start_time"), parsed.get("end_time"), parsed.get("vehicle_type"),
                    ))
                execute_values(cur, """
                    INSERT INTO parking_meters (
                        meter_number, status, pay_by_cell, meter_hours,
                        borough, on_street, side_of_street, from_street, to_street,
                        latitude, longitude, max_hours, days, start_time, end_time, vehicle_type
                    ) VALUES %s
                """, rows, page_size=5000)
                conn.commit()
                total += len(meters)
                print(f"[ingest] Meters: {total}")
                if len(meters) < batch_size:
                    break
                offset += batch_size
        print(f"[ingest] Total meters: {total}")
    finally:
        conn.close()


def match_cameras_to_signs():
    """Match each camera to its surrounding block faces using normalized street names."""
    print("[ingest] Matching cameras to block faces (smart matching)...")
    conn = get_conn()

    try:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE camera_block_faces RESTART IDENTITY")
            conn.commit()

            cur.execute("SELECT id, name, main_street, cross_street, area, latitude, longitude FROM dot_cameras WHERE camera_type = 'intersection'")
            cameras = cur.fetchall()

            matched = 0
            matched_cameras = 0

            for cam_id, cam_name, main_st, cross_st, area, cam_lat, cam_lng in cameras:
                if not main_st or not cross_st or not area:
                    continue

                main_pattern = _build_main_street_sql_pattern(main_st)
                cross_pattern = _build_cross_street_sql_pattern(cross_st)

                # Strategy 1: match by street name patterns
                cur.execute("""
                    SELECT DISTINCT on_street, from_street, to_street, side_of_street,
                        COUNT(*) as sign_count,
                        BOOL_OR(rule_type = 'metered') as has_metered,
                        BOOL_OR(rule_type NOT IN ('no_standing','no_parking','bus_stop','taxi_stand','loading','special_permit')) as has_free,
                        BOOL_OR(rule_type = 'no_parking') as has_no_parking,
                        BOOL_OR(rule_type = 'no_standing') as has_no_standing
                    FROM parking_signs
                    WHERE upper(on_street) LIKE %s
                        AND borough = %s
                        AND (upper(from_street) LIKE %s OR upper(to_street) LIKE %s)
                    GROUP BY on_street, from_street, to_street, side_of_street
                """, (f"%{main_pattern}%", area, cross_pattern, cross_pattern))

                faces = cur.fetchall()

                # Strategy 2: if no matches, try geographic proximity
                # Find signs within ~150m of the camera using lat/lng
                if not faces and cam_lat and cam_lng:
                    lat_delta = 150 / 111000  # ~150m
                    lng_delta = 150 / 85000
                    cur.execute("""
                        SELECT DISTINCT on_street, from_street, to_street, side_of_street,
                            COUNT(*) as sign_count,
                            BOOL_OR(rule_type = 'metered') as has_metered,
                            BOOL_OR(rule_type NOT IN ('no_standing','no_parking','bus_stop','taxi_stand','loading','special_permit')) as has_free,
                            BOOL_OR(rule_type = 'no_parking') as has_no_parking,
                            BOOL_OR(rule_type = 'no_standing') as has_no_standing
                        FROM parking_signs
                        WHERE latitude BETWEEN %s AND %s
                            AND longitude BETWEEN %s AND %s
                            AND borough = %s
                        GROUP BY on_street, from_street, to_street, side_of_street
                        LIMIT 20
                    """, (
                        cam_lat - lat_delta, cam_lat + lat_delta,
                        cam_lng - lng_delta, cam_lng + lng_delta,
                        area,
                    ))
                    faces = cur.fetchall()

                if not faces:
                    continue

                rows = []
                for on_st, from_st, to_st, side, sign_count, has_metered, has_free, has_no_parking, has_no_standing in faces:
                    cur.execute("""
                        SELECT COUNT(*) FROM parking_meters
                        WHERE borough = %s
                          AND latitude BETWEEN %s AND %s
                          AND longitude BETWEEN %s AND %s
                    """, (area, cam_lat - 0.002, cam_lat + 0.002, cam_lng - 0.002, cam_lng + 0.002)) if cam_lat else cur.execute("SELECT 0")
                    meter_count = cur.fetchone()[0]

                    rows.append((
                        cam_id, on_st, from_st, to_st, side, area,
                        sign_count, meter_count, has_metered or False, has_free or False,
                        has_no_parking or False, has_no_standing or False,
                    ))
                    matched += 1

                if rows:
                    execute_values(cur, """
                        INSERT INTO camera_block_faces (
                            camera_id, on_street, from_street, to_street, side_of_street, borough,
                            sign_count, meter_count, has_metered_parking, has_free_parking,
                            has_no_parking, has_no_standing
                        ) VALUES %s
                        ON CONFLICT DO NOTHING
                    """, rows)
                    matched_cameras += 1

            conn.commit()
            print(f"[ingest] Matched {matched} block faces across {matched_cameras} cameras")
    finally:
        conn.close()


def run_full_ingest():
    from app.db import run_migrations
    run_migrations()
    ingest_cameras()
    ingest_signs()
    ingest_meters()
    match_cameras_to_signs()
    print("[ingest] Full ingest complete")
