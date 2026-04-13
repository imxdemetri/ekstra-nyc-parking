"""NYC Parking Intelligence API — built on Ekstra."""

import os
from datetime import datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

from app.db import run_migrations, get_conn
from app.rules import evaluate_parking, can_i_park_all_day


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
