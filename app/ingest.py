"""Ingest NYC DOT cameras, parking signs, and meters into PostGIS."""

import re
import httpx
from pyproj import Transformer
from psycopg2.extras import execute_values
from app.db import get_conn

DOT_CAMERAS_URL = "https://webcams.nyctmc.org/api/cameras"
SIGNS_URL = "https://data.cityofnewyork.us/resource/nfid-uabd.json"
METERS_URL = "https://data.cityofnewyork.us/resource/693u-uax6.json"

# NYS Plane Long Island (feet) to WGS84
_transformer = Transformer.from_crs("EPSG:2263", "EPSG:4326", always_xy=True)


def _nys_to_latlon(x: float, y: float) -> tuple[float, float]:
    lng, lat = _transformer.transform(x, y)
    return lat, lng


def _parse_camera_name(name: str) -> tuple[str, str, str]:
    """Parse 'Amsterdam Ave @ 60 St' into (main_street, cross_street, type)."""
    if "@" in name:
        parts = name.split("@", 1)
        return parts[0].strip(), parts[1].strip(), "intersection"
    return name, "", "highway"


# ── Sign description parser ──────────────────────────────────────────

_DAY_NAMES = {
    "MONDAY": "monday", "TUESDAY": "tuesday", "WEDNESDAY": "wednesday",
    "THURSDAY": "thursday", "FRIDAY": "friday", "SATURDAY": "saturday", "SUNDAY": "sunday",
    "MON": "monday", "TUE": "tuesday", "TUES": "tuesday", "WED": "wednesday",
    "THU": "thursday", "THUR": "thursday", "FRI": "friday", "SAT": "saturday", "SUN": "sunday",
}

_TIME_RE = re.compile(r"(\d{1,2}(?::\d{2})?)\s*(AM|PM)", re.IGNORECASE)
_HOUR_RE = re.compile(r"(\d+)\s*H(?:R|MP)", re.IGNORECASE)
_DAY_RANGE_RE = re.compile(r"(MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY)[\s-]*(THROUGH|THRU|-)?\s*(MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY)?", re.IGNORECASE)


def _parse_time(s: str) -> str | None:
    """Parse '8AM' or '7:30PM' to '08:00' or '19:30'."""
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
    """Extract days from sign description."""
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

    # Individual days mentioned
    found = []
    for token in re.split(r"[\s,/&]+", upper):
        token = token.strip(".-")
        if token in _DAY_NAMES:
            found.append(_DAY_NAMES[token])
    return found if found else ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _parse_sign_rule(desc: str) -> dict:
    """Parse a sign_description into structured parking rule."""
    upper = desc.upper()

    # Determine rule type
    if "NO STANDING" in upper:
        rule_type = "no_standing"
    elif "NO STOPPING" in upper:
        rule_type = "no_standing"
    elif "NO PARKING" in upper and "SANITATION" in upper:
        rule_type = "no_parking"
        is_asp = True
    elif "NO PARKING" in upper:
        rule_type = "no_parking"
        is_asp = False
    elif "HMP" in upper or "METER" in upper or "PAY-BY-CELL" in upper:
        rule_type = "metered"
        is_asp = False
    elif "LOADING" in upper:
        rule_type = "loading"
        is_asp = False
    elif "BUS STOP" in upper or "BUS ONLY" in upper:
        rule_type = "bus_stop"
        is_asp = False
    elif "TAXI" in upper:
        rule_type = "taxi_stand"
        is_asp = False
    elif "AUTHORIZED" in upper or "PERMIT" in upper or "LICENSE PLATES ONLY" in upper:
        rule_type = "special_permit"
        is_asp = False
    else:
        rule_type = "other"
        is_asp = False

    if rule_type == "no_parking" and "SANITATION" in upper:
        is_asp = True

    # Parse times
    times = _TIME_RE.findall(desc)
    start_time = None
    end_time = None
    if len(times) >= 2:
        start_time = _parse_time(f"{times[0][0]} {times[0][1]}")
        end_time = _parse_time(f"{times[1][0]} {times[1][1]}")
    elif len(times) == 1:
        start_time = _parse_time(f"{times[0][0]} {times[0][1]}")

    # Parse max hours
    max_hours = None
    hm = _HOUR_RE.search(desc)
    if hm:
        max_hours = float(hm.group(1))

    # Parse days
    days = _parse_days(desc)

    # Vehicle restriction
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
        "rule_type": rule_type,
        "days": days,
        "start_time": start_time,
        "end_time": end_time,
        "max_hours": max_hours,
        "vehicle_restriction": vehicle_restriction,
        "is_asp": is_asp,
    }


def _parse_meter_hours(hours_str: str) -> dict:
    """Parse '2HR Pas Mon-Sat 0900-1900' into structured rule."""
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

    # Parse 4-digit times like 0900-1900
    time_match = re.search(r"(\d{4})-(\d{4})", upper)
    start_time = None
    end_time = None
    if time_match:
        s, e = time_match.group(1), time_match.group(2)
        start_time = f"{s[:2]}:{s[2:]}"
        end_time = f"{e[:2]}:{e[2:]}"

    return {
        "max_hours": max_hours,
        "vehicle_type": vehicle_type,
        "days": days,
        "start_time": start_time,
        "end_time": end_time,
    }


# ── Ingest functions ─────────────────────────────────────────────────

def ingest_cameras():
    """Fetch 953 DOT cameras and upsert into dot_cameras table."""
    print("[ingest] Fetching DOT cameras...")
    resp = httpx.get(DOT_CAMERAS_URL, timeout=30)
    cameras = resp.json()
    print(f"[ingest] Got {len(cameras)} cameras")

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            for cam in cameras:
                main_st, cross_st, cam_type = _parse_camera_name(cam["name"])
                cur.execute("""
                    INSERT INTO dot_cameras (id, name, latitude, longitude, area, is_online, main_street, cross_street, camera_type, image_url, last_synced_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (id) DO UPDATE SET
                        is_online = EXCLUDED.is_online,
                        last_synced_at = NOW()
                """, (
                    cam["id"], cam["name"], cam["latitude"], cam["longitude"],
                    cam.get("area"), cam.get("isOnline") == "true",
                    main_st, cross_st, cam_type, cam.get("imageUrl"),
                ))
        conn.commit()
        print(f"[ingest] Upserted {len(cameras)} cameras")
    finally:
        conn.close()


def ingest_signs(batch_size: int = 5000):
    """Fetch all 440K parking signs and insert into parking_signs table."""
    print("[ingest] Fetching parking signs...")
    conn = get_conn()
    total = 0
    offset = 0

    try:
        with conn.cursor() as cur:
            # Clear and reload
            cur.execute("DELETE FROM parking_signs")
            conn.commit()

            while True:
                url = f"{SIGNS_URL}?$limit={batch_size}&$offset={offset}&$order=order_number"
                resp = httpx.get(url, timeout=60)
                signs = resp.json()
                if not signs:
                    break

                rows = []
                for s in signs:
                    # Convert coordinates
                    lat, lng = None, None
                    x = s.get("sign_x_coord")
                    y = s.get("sign_y_coord")
                    if x and y:
                        try:
                            lat, lng = _nys_to_latlon(float(x), float(y))
                        except Exception:
                            pass

                    # Parse the rule
                    rule = _parse_sign_rule(s.get("sign_description", ""))

                    rows.append((
                        s.get("order_number"),
                        s.get("borough"),
                        s.get("on_street", ""),
                        s.get("from_street", ""),
                        s.get("to_street", ""),
                        s.get("side_of_street"),
                        s.get("sign_code"),
                        s.get("sign_description", ""),
                        int(s["distance_from_intersection"]) if s.get("distance_from_intersection") else None,
                        s.get("arrow_direction"),
                        float(x) if x else None,
                        float(y) if y else None,
                        lat, lng,
                        rule["rule_type"],
                        rule["days"] or None,
                        rule.get("start_time"),
                        rule.get("end_time"),
                        rule.get("max_hours"),
                        rule.get("vehicle_restriction"),
                        rule.get("is_asp", False),
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
                """, rows)
                conn.commit()

                total += len(signs)
                print(f"[ingest] Signs: {total} ingested (offset {offset})")
                offset += batch_size

        print(f"[ingest] Total signs ingested: {total}")
    finally:
        conn.close()


def ingest_meters(batch_size: int = 5000):
    """Fetch all 15K parking meters."""
    print("[ingest] Fetching parking meters...")
    conn = get_conn()
    total = 0
    offset = 0

    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM parking_meters")
            conn.commit()

            while True:
                url = f"{METERS_URL}?$limit={batch_size}&$offset={offset}"
                resp = httpx.get(url, timeout=60)
                meters = resp.json()
                if not meters:
                    break

                rows = []
                for m in meters:
                    parsed = _parse_meter_hours(m.get("meter_hours", ""))
                    lat = float(m["lat"]) if m.get("lat") else None
                    lng = float(m["long"]) if m.get("long") else None

                    rows.append((
                        m.get("meter_number"),
                        m.get("status"),
                        m.get("pay_by_cell_number"),
                        m.get("meter_hours"),
                        m.get("borough"),
                        m.get("on_street"),
                        m.get("side_of_street"),
                        m.get("from_street"),
                        m.get("to_street"),
                        lat, lng,
                        parsed.get("max_hours"),
                        parsed.get("days"),
                        parsed.get("start_time"),
                        parsed.get("end_time"),
                        parsed.get("vehicle_type"),
                    ))

                execute_values(cur, """
                    INSERT INTO parking_meters (
                        meter_number, status, pay_by_cell, meter_hours,
                        borough, on_street, side_of_street, from_street, to_street,
                        latitude, longitude,
                        max_hours, days, start_time, end_time, vehicle_type
                    ) VALUES %s
                """, rows)
                conn.commit()

                total += len(meters)
                print(f"[ingest] Meters: {total} ingested")
                offset += batch_size

        print(f"[ingest] Total meters ingested: {total}")
    finally:
        conn.close()


def match_cameras_to_signs():
    """For each intersection camera, find parking signs on surrounding block faces."""
    print("[ingest] Matching cameras to block faces...")
    conn = get_conn()

    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM camera_block_faces")
            conn.commit()

            # Get all intersection cameras
            cur.execute("SELECT id, name, main_street, cross_street, area FROM dot_cameras WHERE camera_type = 'intersection'")
            cameras = cur.fetchall()

            matched = 0
            for cam_id, cam_name, main_st, cross_st, area in cameras:
                if not main_st or not area:
                    continue

                # Normalize street names for matching
                main_upper = main_st.upper().strip()

                # Find signs on the main street in this borough, near the cross street
                cur.execute("""
                    SELECT DISTINCT on_street, from_street, to_street, side_of_street,
                        COUNT(*) as sign_count,
                        BOOL_OR(rule_type = 'metered') as has_metered,
                        BOOL_OR(rule_type = 'free' OR (rule_type = 'other' AND sign_description NOT LIKE '%%NO %%')) as has_free,
                        BOOL_OR(rule_type = 'no_parking') as has_no_parking,
                        BOOL_OR(rule_type = 'no_standing') as has_no_standing
                    FROM parking_signs
                    WHERE upper(on_street) LIKE %s
                        AND borough = %s
                        AND (upper(from_street) LIKE %s OR upper(to_street) LIKE %s)
                    GROUP BY on_street, from_street, to_street, side_of_street
                """, (
                    f"%{main_upper}%", area,
                    f"%{cross_st.upper().strip()}%" if cross_st else "%",
                    f"%{cross_st.upper().strip()}%" if cross_st else "%",
                ))

                faces = cur.fetchall()
                for on_st, from_st, to_st, side, sign_count, has_metered, has_free, has_no_parking, has_no_standing in faces:
                    # Count nearby meters
                    cur.execute("""
                        SELECT COUNT(*) FROM parking_meters
                        WHERE upper(on_street) LIKE %s
                            AND borough = %s
                            AND (upper(from_street) LIKE %s OR upper(to_street) LIKE %s)
                    """, (f"%{main_upper}%", area, f"%{from_st.upper()}%", f"%{to_st.upper()}%"))
                    meter_count = cur.fetchone()[0]

                    cur.execute("""
                        INSERT INTO camera_block_faces (
                            camera_id, on_street, from_street, to_street, side_of_street, borough,
                            sign_count, meter_count, has_metered_parking, has_free_parking,
                            has_no_parking, has_no_standing
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT DO NOTHING
                    """, (
                        cam_id, on_st, from_st, to_st, side, area,
                        sign_count, meter_count, has_metered or False, has_free or False,
                        has_no_parking or False, has_no_standing or False,
                    ))
                    matched += 1

            conn.commit()
            print(f"[ingest] Matched {matched} block faces to cameras")
    finally:
        conn.close()


def run_full_ingest():
    """Run the complete ingest pipeline."""
    from app.db import run_migrations
    run_migrations()
    ingest_cameras()
    ingest_signs()
    ingest_meters()
    match_cameras_to_signs()
    print("[ingest] Full ingest complete")
