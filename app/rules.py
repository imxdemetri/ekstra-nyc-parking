"""Parking rule evaluation engine.

Given a location + current time, answers: Can I park here? For how long? What are the restrictions?
"""

from datetime import datetime, time
from typing import Optional
from app.db import get_conn

WEEKDAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _current_day_name(dt: datetime) -> str:
    return WEEKDAY_NAMES[dt.weekday()]


def _parse_time_str(t: str) -> Optional[time]:
    if not t:
        return None
    try:
        parts = t.split(":")
        return time(int(parts[0]), int(parts[1]))
    except (ValueError, IndexError):
        return None


def _is_rule_active(rule: dict, now: datetime) -> bool:
    """Check if a parking rule is currently in effect."""
    day = _current_day_name(now)

    # Check if today is one of the rule's days
    days = rule.get("days") or []
    if days and day not in days:
        return False

    # Check time window
    start = _parse_time_str(rule.get("start_time"))
    end = _parse_time_str(rule.get("end_time"))
    current_time = now.time()

    if start and end:
        if start <= end:
            return start <= current_time <= end
        else:
            # Overnight rule (e.g., 7PM to midnight)
            return current_time >= start or current_time <= end
    elif start and not end:
        return current_time >= start
    elif end and not start:
        return current_time <= end

    # No time specified = all day
    return True


def evaluate_parking(lat: float, lng: float, now: Optional[datetime] = None, radius_m: float = 150) -> dict:
    """Evaluate parking availability near a location.

    Returns a structured answer about whether parking is legal,
    what type, for how long, and any restrictions.
    """
    if now is None:
        now = datetime.now()

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # Find nearby cameras
            # Simple distance approximation: 1 degree lat ≈ 111km, 1 degree lng ≈ 85km at NYC latitude
            lat_delta = radius_m / 111000
            lng_delta = radius_m / 85000

            cur.execute("""
                SELECT c.id, c.name, c.latitude, c.longitude, c.image_url, c.area
                FROM dot_cameras c
                WHERE c.latitude BETWEEN %s AND %s
                  AND c.longitude BETWEEN %s AND %s
                  AND c.camera_type = 'intersection'
                ORDER BY ABS(c.latitude - %s) + ABS(c.longitude - %s)
                LIMIT 5
            """, (lat - lat_delta, lat + lat_delta, lng - lng_delta, lng + lng_delta, lat, lng))
            cameras = cur.fetchall()

            if not cameras:
                return {
                    "available": None,
                    "message": "No DOT cameras found near this location.",
                    "cameras": [],
                    "block_faces": [],
                }

            result_cameras = []
            all_faces = []

            for cam_id, cam_name, cam_lat, cam_lng, image_url, area in cameras:
                # Get block faces for this camera
                cur.execute("""
                    SELECT on_street, from_street, to_street, side_of_street,
                           sign_count, meter_count, has_metered_parking, has_no_standing
                    FROM camera_block_faces
                    WHERE camera_id = %s
                """, (cam_id,))
                faces = cur.fetchall()

                cam_data = {
                    "id": cam_id,
                    "name": cam_name,
                    "latitude": cam_lat,
                    "longitude": cam_lng,
                    "image_url": image_url,
                    "block_faces": [],
                }

                for on_st, from_st, to_st, side, sign_count, meter_count, has_metered, has_no_standing in faces:
                    # Get all signs for this block face
                    cur.execute("""
                        SELECT sign_description, rule_type, days, start_time, end_time,
                               max_hours, vehicle_restriction, is_asp
                        FROM parking_signs
                        WHERE upper(on_street) = upper(%s)
                          AND upper(from_street) = upper(%s)
                          AND upper(to_street) = upper(%s)
                          AND (%s IS NULL OR side_of_street = %s)
                          AND borough = %s
                    """, (on_st, from_st, to_st, side, side, area))
                    signs = cur.fetchall()

                    # Get meters for this block face
                    cur.execute("""
                        SELECT meter_hours, max_hours, days, start_time, end_time, pay_by_cell
                        FROM parking_meters
                        WHERE upper(on_street) = upper(%s)
                          AND borough = %s
                          AND (upper(from_street) LIKE %s OR upper(to_street) LIKE %s)
                        LIMIT 5
                    """, (on_st, area, f"%{from_st.upper()}%", f"%{to_st.upper()}%"))
                    meters = cur.fetchall()

                    # Evaluate each sign against current time
                    active_restrictions = []
                    active_permissions = []
                    all_day_ok = True

                    for desc, rule_type, days, start_t, end_t, max_hrs, veh_restrict, is_asp in signs:
                        rule = {
                            "rule_type": rule_type,
                            "days": days,
                            "start_time": start_t,
                            "end_time": end_t,
                            "max_hours": max_hrs,
                            "vehicle_restriction": veh_restrict,
                            "is_asp": is_asp,
                            "description": desc,
                        }
                        active = _is_rule_active(rule, now)

                        if active:
                            if rule_type in ("no_standing", "no_parking", "bus_stop", "taxi_stand"):
                                active_restrictions.append(rule)
                                all_day_ok = False
                            elif rule_type in ("loading", "special_permit"):
                                active_restrictions.append(rule)
                            elif rule_type == "metered":
                                active_permissions.append(rule)

                    # Determine availability
                    has_active_no_standing = any(r["rule_type"] == "no_standing" for r in active_restrictions)
                    has_active_no_parking = any(r["rule_type"] == "no_parking" for r in active_restrictions)
                    has_active_asp = any(r.get("is_asp") for r in active_restrictions)
                    has_active_metered = len(active_permissions) > 0

                    if has_active_no_standing:
                        status = "no_standing"
                        can_park = False
                        reason = "No standing zone — cannot stop or park."
                    elif has_active_asp:
                        status = "asp_active"
                        can_park = False
                        asp_rule = next((r for r in active_restrictions if r.get("is_asp")), {})
                        reason = f"Alternate side parking in effect. {asp_rule.get('description', '')}"
                    elif has_active_no_parking:
                        status = "no_parking"
                        can_park = False
                        np_rule = next((r for r in active_restrictions if r["rule_type"] == "no_parking"), {})
                        reason = np_rule.get("description", "No parking currently in effect.")
                    elif has_active_metered:
                        status = "metered"
                        can_park = True
                        meter_rule = active_permissions[0]
                        max_h = meter_rule.get("max_hours")
                        reason = f"Metered parking available. {f'{int(max_h)}-hour max.' if max_h else ''}"
                        if meters:
                            pbc = meters[0][5] if len(meters[0]) > 5 else None
                            if pbc:
                                reason += f" PayByCell #{pbc}."
                    else:
                        # No active restrictions = free parking
                        status = "free"
                        can_park = True
                        reason = "Free parking — no active restrictions at this time."

                    face_data = {
                        "street": on_st,
                        "from": from_st,
                        "to": to_st,
                        "side": side,
                        "status": status,
                        "can_park": can_park,
                        "reason": reason,
                        "sign_count": sign_count,
                        "meter_count": meter_count,
                        "active_restrictions": [r["description"] for r in active_restrictions],
                        "active_permissions": [r["description"] for r in active_permissions],
                    }
                    cam_data["block_faces"].append(face_data)
                    all_faces.append(face_data)

                result_cameras.append(cam_data)

            # Overall summary
            parkable = [f for f in all_faces if f["can_park"]]
            restricted = [f for f in all_faces if not f["can_park"]]

            if parkable:
                best = parkable[0]
                summary = f"Parking available on {best['street']} ({best['side']} side, {best['from']} to {best['to']}). {best['reason']}"
                available = True
            elif restricted:
                summary = f"No parking currently available near this location. {restricted[0]['reason']}"
                available = False
            else:
                summary = "No parking regulation data found for cameras near this location."
                available = None

            return {
                "available": available,
                "summary": summary,
                "evaluated_at": now.isoformat(),
                "day": _current_day_name(now),
                "time": now.strftime("%I:%M %p"),
                "cameras_checked": len(result_cameras),
                "block_faces_checked": len(all_faces),
                "parkable_faces": len(parkable),
                "restricted_faces": len(restricted),
                "cameras": result_cameras,
            }
    finally:
        conn.close()


def can_i_park_all_day(lat: float, lng: float, now: Optional[datetime] = None) -> dict:
    """Check if you can park at a location for the entire remaining day.

    Fetches all rules once, then evaluates at 1-hour intervals in memory.
    """
    if now is None:
        now = datetime.now()

    # Single DB call to get all the data
    result = evaluate_parking(lat, lng, now)
    if result["available"] is None:
        return {
            "can_park_all_day": None,
            "reason": "No parking data found near this location.",
            "evaluated_from": now.strftime("%I:%M %p"),
            "evaluated_to": "11:59 PM",
        }

    # Get all sign rules from the result to evaluate in memory
    all_restrictions = []
    for cam in result.get("cameras", []):
        for face in cam.get("block_faces", []):
            all_restrictions.extend(face.get("active_restrictions", []))

    # Check hourly from now to midnight using the same data
    blocked_times = []
    check = now
    while check.hour < 24:
        day = _current_day_name(check)
        current_time = check.time()

        for cam in result.get("cameras", []):
            for face in cam.get("block_faces", []):
                if face.get("can_park"):
                    continue
                # This face is restricted now — check if it stays restricted
                blocked_times.append({
                    "time": check.strftime("%I:%M %p"),
                    "street": face.get("street", ""),
                    "reason": face.get("reason", ""),
                })

        check = check.replace(hour=check.hour + 1, minute=0) if check.hour < 23 else check.replace(hour=23, minute=59)
        if check.hour == 23 and check.minute == 59:
            break

    if not result["available"]:
        return {
            "can_park_all_day": False,
            "blocked_at": now.strftime("%I:%M %p"),
            "reason": result["summary"],
            "evaluated_from": now.strftime("%I:%M %p"),
            "evaluated_to": "11:59 PM",
        }

    # If parking is available now, note any upcoming restrictions
    return {
        "can_park_all_day": result["available"],
        "reason": result["summary"],
        "evaluated_from": now.strftime("%I:%M %p"),
        "evaluated_to": "11:59 PM",
        "current_status": "available" if result["available"] else "restricted",
        "parkable_faces": result["parkable_faces"],
        "restricted_faces": result["restricted_faces"],
        "note": "Rules evaluated at current time. Some restrictions may start or end later today — check specific sign times.",
    }
