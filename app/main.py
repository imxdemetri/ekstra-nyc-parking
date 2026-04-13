"""NYC Parking Intelligence API — built on Ekstra."""

import os
from datetime import datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

from app.db import run_migrations, get_conn
from app.rules import evaluate_parking, can_i_park_all_day
from app.vision import scan_cameras, detect_vehicles


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Run migrations only — ingest is triggered via POST /api/v1/parking/ingest
    try:
        run_migrations()
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM dot_cameras")
            cam_count = cur.fetchone()[0]
        conn.close()
        print(f"[startup] DB ready. {cam_count} cameras in database. POST /api/v1/parking/ingest to load data.")
    except Exception as e:
        print(f"[startup] DB connection issue: {e}")
    yield


app = FastAPI(
    title="Ekstra NYC Parking Intelligence",
    description="Real-time parking legality for every block in NYC. 953 DOT cameras, 440K signs, 15K meters.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://ekstra.ai", "http://localhost:3000", "http://localhost:3004"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok", "service": "nyc-parking"}


@app.get("/api/v1/parking/near")
def parking_near(
    lat: float = Query(..., description="Latitude"),
    lng: float = Query(..., description="Longitude"),
    radius: float = Query(150, description="Search radius in meters"),
):
    """Find parking availability near a location."""
    now = datetime.now()
    result = evaluate_parking(lat, lng, now, radius)
    return result


@app.get("/api/v1/parking/all-day")
def parking_all_day(
    lat: float = Query(..., description="Latitude"),
    lng: float = Query(..., description="Longitude"),
):
    """Check if you can park at a location for the rest of the day."""
    now = datetime.now()
    result = can_i_park_all_day(lat, lng, now)
    return result


@app.get("/api/v1/parking/cameras")
def list_cameras(
    area: str = Query(None, description="Filter by borough"),
    has_parking: bool = Query(None, description="Only cameras with parkable block faces"),
):
    """List DOT cameras with parking regulation data."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            conditions = ["1=1"]
            params = []

            if area:
                conditions.append("c.area = %s")
                params.append(area)

            if has_parking is not None:
                if has_parking:
                    conditions.append("EXISTS (SELECT 1 FROM camera_block_faces cbf WHERE cbf.camera_id = c.id AND (cbf.has_metered_parking OR cbf.has_free_parking))")
                else:
                    conditions.append("NOT EXISTS (SELECT 1 FROM camera_block_faces cbf WHERE cbf.camera_id = c.id)")

            where = " AND ".join(conditions)
            cur.execute(f"""
                SELECT c.id, c.name, c.latitude, c.longitude, c.area, c.image_url, c.camera_type,
                       (SELECT COUNT(*) FROM camera_block_faces cbf WHERE cbf.camera_id = c.id) as face_count,
                       (SELECT BOOL_OR(cbf.has_metered_parking) FROM camera_block_faces cbf WHERE cbf.camera_id = c.id) as any_metered
                FROM dot_cameras c
                WHERE {where}
                ORDER BY c.area, c.name
            """, params)

            cameras = []
            for row in cur.fetchall():
                cameras.append({
                    "id": row[0],
                    "name": row[1],
                    "latitude": row[2],
                    "longitude": row[3],
                    "area": row[4],
                    "image_url": row[5],
                    "type": row[6],
                    "block_faces": row[7],
                    "has_metered": row[8] or False,
                })

            return {"cameras": cameras, "count": len(cameras)}
    finally:
        conn.close()


@app.get("/api/v1/parking/cameras/{camera_id}")
def camera_detail(camera_id: str):
    """Get parking rules for a specific camera's block faces."""
    now = datetime.now()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, latitude, longitude, area, image_url FROM dot_cameras WHERE id = %s", (camera_id,))
            cam = cur.fetchone()
            if not cam:
                return {"error": "Camera not found"}

            # Use the same evaluation logic
            result = evaluate_parking(cam[2], cam[3], now, radius_m=50)
            result["camera"] = {
                "id": cam[0],
                "name": cam[1],
                "latitude": cam[2],
                "longitude": cam[3],
                "area": cam[4],
                "image_url": cam[5],
            }
            return result
    finally:
        conn.close()


@app.get("/api/v1/parking/scan")
def scan_nearby(
    lat: float = Query(..., description="Latitude"),
    lng: float = Query(..., description="Longitude"),
    radius: float = Query(300, description="Search radius in meters"),
    limit: int = Query(10, description="Max cameras to scan"),
):
    """Real-time scan: detect vehicles on nearby DOT cameras + evaluate parking rules.

    Combines YOLOv8n vehicle detection with regulation data for a complete picture:
    - How many vehicles are visible on camera right now
    - Whether parking is legally allowed at this time
    - Traffic status (empty/light/moderate/heavy)
    """
    now = datetime.now()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            lat_delta = radius / 111000
            lng_delta = radius / 85000
            cur.execute("""
                SELECT id, name, image_url, latitude, longitude
                FROM dot_cameras
                WHERE latitude BETWEEN %s AND %s
                  AND longitude BETWEEN %s AND %s
                  AND camera_type = 'intersection'
                ORDER BY ABS(latitude - %s) + ABS(longitude - %s)
                LIMIT %s
            """, (lat - lat_delta, lat + lat_delta, lng - lng_delta, lng + lng_delta, lat, lng, limit))
            cameras = cur.fetchall()
    finally:
        conn.close()

    if not cameras:
        return {"cameras_scanned": 0, "message": "No DOT cameras found nearby.", "results": []}

    cam_tuples = [(c[0], c[1], c[2], c[3], c[4]) for c in cameras]
    detections = scan_cameras(cam_tuples)

    # Enrich with parking rules
    rules = evaluate_parking(lat, lng, now, radius)

    # Merge: for each detection, find matching rule data
    for det in detections:
        matching_cam = next((c for c in rules.get("cameras", []) if c["id"] == det["camera_id"]), None)
        if matching_cam:
            det["block_faces"] = matching_cam.get("block_faces", [])
            parkable = [f for f in det["block_faces"] if f.get("can_park")]
            det["legal_parking_available"] = len(parkable) > 0
            if parkable:
                det["parking_summary"] = parkable[0]["reason"]
            else:
                restricted = [f for f in det["block_faces"] if not f.get("can_park")]
                det["parking_summary"] = restricted[0]["reason"] if restricted else "No regulation data"
        else:
            det["block_faces"] = []
            det["legal_parking_available"] = None
            det["parking_summary"] = "No regulation data for this camera"

    available = [d for d in detections if d.get("legal_parking_available") and d["vehicles_detected"] <= 3]

    summary = ""
    if available:
        best = available[0]
        summary = f"Parking likely available at {best['camera_name']}. {best['vehicles_detected']} vehicles visible. {best['parking_summary']}"
    elif detections:
        summary = f"Scanned {len(detections)} cameras. All locations are either congested or restricted right now."
    else:
        summary = "Could not scan any cameras near this location."

    return {
        "summary": summary,
        "scanned_at": now.isoformat(),
        "day": now.strftime("%A"),
        "time": now.strftime("%I:%M %p"),
        "cameras_scanned": len(detections),
        "parking_available": len(available),
        "results": detections,
    }


@app.get("/api/v1/parking/camera/{camera_id}/live")
def camera_live_scan(camera_id: str):
    """Scan a single camera for vehicles right now."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, image_url, latitude, longitude FROM dot_cameras WHERE id = %s", (camera_id,))
            cam = cur.fetchone()
            if not cam:
                return {"error": "Camera not found"}
    finally:
        conn.close()

    detection = detect_vehicles(cam[2])
    if detection is None:
        return {"error": "Could not fetch or analyze camera image"}

    now = datetime.now()
    rules = evaluate_parking(cam[3], cam[4], now, radius_m=50)

    return {
        "camera": {"id": cam[0], "name": cam[1], "image_url": cam[2], "latitude": cam[3], "longitude": cam[4]},
        "detection": detection,
        "traffic_status": "empty" if detection["total"] == 0 else "light" if detection["total"] <= 2 else "moderate" if detection["total"] <= 5 else "heavy",
        "parking_rules": rules,
        "scanned_at": now.isoformat(),
    }


@app.post("/api/v1/parking/ingest")
def trigger_ingest(
    cameras: bool = Query(True, description="Ingest DOT cameras"),
    signs: bool = Query(True, description="Ingest parking signs"),
    meters: bool = Query(True, description="Ingest parking meters"),
    match: bool = Query(True, description="Match cameras to block faces"),
):
    """Manually trigger data ingest. Can run individual steps."""
    import traceback
    from app.ingest import ingest_cameras, ingest_signs, ingest_meters, match_cameras_to_signs
    from app.db import run_migrations
    try:
        run_migrations()
        results = {}
        if cameras:
            ingest_cameras()
            results["cameras"] = "done"
        if signs:
            ingest_signs()
            results["signs"] = "done"
        if meters:
            ingest_meters()
            results["meters"] = "done"
        if match:
            match_cameras_to_signs()
            results["matching"] = "done"
        return {"status": "ok", "steps": results}
    except Exception as e:
        tb = traceback.format_exc()
        print(f"[ingest] ERROR: {tb}")
        return {"status": "error", "message": str(e), "traceback": tb}


@app.get("/api/v1/parking/stats")
def parking_stats():
    """Get overall parking data statistics."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            stats = {}
            cur.execute("SELECT COUNT(*) FROM dot_cameras")
            stats["cameras"] = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM dot_cameras WHERE camera_type = 'intersection'")
            stats["intersection_cameras"] = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM parking_signs")
            stats["signs"] = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM parking_meters")
            stats["meters"] = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM camera_block_faces")
            stats["camera_block_faces"] = cur.fetchone()[0]
            cur.execute("SELECT COUNT(DISTINCT camera_id) FROM camera_block_faces")
            stats["cameras_with_parking_data"] = cur.fetchone()[0]

            cur.execute("SELECT area, COUNT(*) FROM dot_cameras GROUP BY area ORDER BY COUNT(*) DESC")
            stats["cameras_by_borough"] = {row[0]: row[1] for row in cur.fetchall()}

            return stats
    finally:
        conn.close()
