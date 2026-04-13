"""Database connection and schema for NYC parking intelligence."""

import os
import psycopg2
from psycopg2.extras import execute_values

DATABASE_URL = os.environ.get("DATABASE_URL", "")

def get_conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require" if "railway" in DATABASE_URL else "prefer")

SCHEMA_SQL = """
-- DOT traffic cameras
CREATE TABLE IF NOT EXISTS dot_cameras (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    latitude DOUBLE PRECISION NOT NULL,
    longitude DOUBLE PRECISION NOT NULL,
    area TEXT,
    is_online BOOLEAN DEFAULT TRUE,
    main_street TEXT,
    cross_street TEXT,
    camera_type TEXT DEFAULT 'intersection',
    image_url TEXT,
    last_synced_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_dot_cameras_geo ON dot_cameras (latitude, longitude);
CREATE INDEX IF NOT EXISTS idx_dot_cameras_area ON dot_cameras (area);

-- Parking regulation signs (from NYC Open Data nfid-uabd)
CREATE TABLE IF NOT EXISTS parking_signs (
    id SERIAL PRIMARY KEY,
    order_number TEXT,
    borough TEXT,
    on_street TEXT NOT NULL,
    from_street TEXT NOT NULL,
    to_street TEXT NOT NULL,
    side_of_street TEXT,
    sign_code TEXT,
    sign_description TEXT NOT NULL,
    distance_from_intersection INTEGER,
    arrow_direction TEXT,
    sign_x DOUBLE PRECISION,
    sign_y DOUBLE PRECISION,
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    -- Parsed rule fields
    rule_type TEXT,           -- metered, no_parking, no_standing, loading, bus_stop, special_permit, free
    days TEXT[],              -- {monday,tuesday,...}
    start_time TEXT,          -- 08:00
    end_time TEXT,            -- 19:00
    max_hours REAL,           -- 2.0
    vehicle_restriction TEXT, -- commercial, doctor, authorized, etc.
    is_asp BOOLEAN DEFAULT FALSE,
    last_synced_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_parking_signs_street ON parking_signs (upper(on_street), borough);
CREATE INDEX IF NOT EXISTS idx_parking_signs_geo ON parking_signs (latitude, longitude) WHERE latitude IS NOT NULL;

-- Parking meters (from NYC Open Data 693u-uax6)
CREATE TABLE IF NOT EXISTS parking_meters (
    id SERIAL PRIMARY KEY,
    meter_number TEXT,
    status TEXT,
    pay_by_cell TEXT,
    meter_hours TEXT,
    borough TEXT,
    on_street TEXT,
    side_of_street TEXT,
    from_street TEXT,
    to_street TEXT,
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    -- Parsed fields
    max_hours REAL,
    days TEXT[],
    start_time TEXT,
    end_time TEXT,
    vehicle_type TEXT,        -- passenger, commercial
    last_synced_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_parking_meters_geo ON parking_meters (latitude, longitude);

-- Camera-to-block-face associations
CREATE TABLE IF NOT EXISTS camera_block_faces (
    id SERIAL PRIMARY KEY,
    camera_id TEXT REFERENCES dot_cameras(id),
    on_street TEXT NOT NULL,
    from_street TEXT NOT NULL,
    to_street TEXT NOT NULL,
    side_of_street TEXT,
    borough TEXT,
    sign_count INTEGER DEFAULT 0,
    meter_count INTEGER DEFAULT 0,
    has_metered_parking BOOLEAN DEFAULT FALSE,
    has_free_parking BOOLEAN DEFAULT FALSE,
    has_no_parking BOOLEAN DEFAULT FALSE,
    has_no_standing BOOLEAN DEFAULT FALSE,
    UNIQUE(camera_id, on_street, from_street, to_street, side_of_street)
);
CREATE INDEX IF NOT EXISTS idx_camera_block_faces_camera ON camera_block_faces (camera_id);
"""

def run_migrations():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
        conn.commit()
        print("[db] Migrations complete")
    finally:
        conn.close()
