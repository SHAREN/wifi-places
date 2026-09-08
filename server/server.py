#!/usr/bin/env python3
import argparse
import hmac
import json
import math
import os
import sqlite3
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from smart_location import classify_scan, init_smart_schema

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = BASE_DIR / "config.json"
DEFAULT_DB = BASE_DIR / "data" / "wifi_location.sqlite3"
MAX_BODY = 4 * 1024 * 1024

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_uuid TEXT NOT NULL UNIQUE,
    device_id TEXT NOT NULL,
    captured_at_ms INTEGER NOT NULL,
    received_at_ms INTEGER NOT NULL,
    latitude REAL,
    longitude REAL,
    accuracy_m REAL,
    altitude_m REAL,
    location_time_ms INTEGER,
    connected_bssid TEXT,
    connected_ssid TEXT,
    app_version TEXT,
    source TEXT NOT NULL DEFAULT 'wigle-fork'
);
CREATE INDEX IF NOT EXISTS idx_scans_captured_at ON scans(captured_at_ms);
CREATE INDEX IF NOT EXISTS idx_scans_device_time ON scans(device_id, captured_at_ms);

CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
    bssid TEXT NOT NULL,
    ssid TEXT,
    rssi INTEGER,
    frequency_mhz INTEGER,
    capabilities TEXT,
    seen_at_ms INTEGER,
    is_connected INTEGER NOT NULL DEFAULT 0,
    UNIQUE(scan_id, bssid)
);
CREATE INDEX IF NOT EXISTS idx_observations_bssid ON observations(bssid);
CREATE INDEX IF NOT EXISTS idx_observations_scan ON observations(scan_id);

CREATE TABLE IF NOT EXISTS places (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    latitude REAL,
    longitude REAL,
    radius_m REAL NOT NULL DEFAULT 150,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS place_fingerprint (
    place_id INTEGER NOT NULL REFERENCES places(id) ON DELETE CASCADE,
    bssid TEXT NOT NULL,
    seen_scans INTEGER NOT NULL DEFAULT 0,
    total_scans INTEGER NOT NULL DEFAULT 0,
    avg_rssi REAL,
    last_seen_ms INTEGER,
    weight REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY(place_id, bssid)
);

CREATE TABLE IF NOT EXISTS visits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    centroid_lat REAL,
    centroid_lon REAL,
    scan_count INTEGER NOT NULL,
    place_id INTEGER REFERENCES places(id) ON DELETE SET NULL,
    confidence REAL,
    fingerprint_json TEXT NOT NULL,
    analysis_run_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_visits_start ON visits(start_ms);
"""


def load_config(path: Path):
    with path.open("r", encoding="utf-8") as f:
        cfg = json.load(f)
    token = str(cfg.get("ingest_token", "")).strip()
    if len(token) < 32:
        raise RuntimeError("config.json must contain a strong ingest_token")
    return cfg


def connect(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path, timeout=20)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=20000")
    return db


def init_db(db_path: Path):
    with connect(db_path) as db:
        db.executescript(SCHEMA)
        init_smart_schema(db)
        db.commit()


def norm_bssid(value):
    if value is None:
        return None
    s = str(value).strip().upper()
    return s if s else None


def safe_float(v):
    try:
        if v is None:
            return None
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def safe_int(v):
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def validate_scan(item):
    if not isinstance(item, dict):
        raise ValueError("scan must be an object")
    scan_uuid = str(item.get("scan_uuid") or "").strip()
    device_id = str(item.get("device_id") or "").strip()
    captured_at_ms = safe_int(item.get("captured_at_ms"))
    if not scan_uuid or len(scan_uuid) > 128:
        raise ValueError("scan_uuid is required")
    if not device_id or len(device_id) > 128:
        raise ValueError("device_id is required")
    if not captured_at_ms or captured_at_ms < 946684800000:
        raise ValueError("captured_at_ms is invalid")
    location = item.get("location") or {}
    wifi = item.get("wifi") or []
    if not isinstance(location, dict) or not isinstance(wifi, list):
        raise ValueError("location/wifi has invalid type")
    if len(wifi) > 2000:
        raise ValueError("too many wifi observations in one scan")
    return scan_uuid, device_id, captured_at_ms, location, wifi


def insert_scan(db, item):
    scan_uuid, device_id, captured_at_ms, location, wifi = validate_scan(item)
    received_at_ms = int(time.time() * 1000)
    lat = safe_float(location.get("latitude"))
    lon = safe_float(location.get("longitude"))
    accuracy = safe_float(location.get("accuracy_m"))
    altitude = safe_float(location.get("altitude_m"))
    loc_time = safe_int(location.get("time_ms"))
    connected_bssid = norm_bssid(item.get("connected_bssid"))
    connected_ssid = item.get("connected_ssid")
    app_version = item.get("app_version")

    cur = db.execute(
        """INSERT OR IGNORE INTO scans
        (scan_uuid, device_id, captured_at_ms, received_at_ms, latitude, longitude,
         accuracy_m, altitude_m, location_time_ms, connected_bssid, connected_ssid, app_version, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (scan_uuid, device_id, captured_at_ms, received_at_ms, lat, lon, accuracy,
         altitude, loc_time, connected_bssid, connected_ssid, str(app_version or "")[:64],
         str(item.get("source") or "wigle-fork")[:64]),
    )
    duplicate = cur.rowcount == 0
    row = db.execute("SELECT id FROM scans WHERE scan_uuid=?", (scan_uuid,)).fetchone()
    scan_id = int(row["id"])
    if duplicate:
        return False, 0, scan_id

    obs_count = 0
    for obs in wifi:
        if not isinstance(obs, dict):
            continue
        bssid = norm_bssid(obs.get("bssid"))
        if not bssid:
            continue
        ssid = obs.get("ssid")
        rssi = safe_int(obs.get("rssi"))
        freq = safe_int(obs.get("frequency_mhz"))
        seen_at = safe_int(obs.get("seen_at_ms"))
        is_connected = 1 if obs.get("is_connected") or bssid == connected_bssid else 0
        caps = obs.get("capabilities")
        db.execute(
            """INSERT OR IGNORE INTO observations
            (scan_id,bssid,ssid,rssi,frequency_mhz,capabilities,seen_at_ms,is_connected)
            VALUES (?,?,?,?,?,?,?,?)""",
            (scan_id, bssid, None if ssid is None else str(ssid)[:256], rssi, freq,
             None if caps is None else str(caps)[:1024], seen_at, is_connected),
        )
        obs_count += 1
    return True, obs_count, scan_id


class Handler(BaseHTTPRequestHandler):
    server_version = "WifiLocation/1.0"

    def log_message(self, fmt, *args):
        print("%s - - [%s] %s" % (self.client_address[0], self.log_date_time_string(), fmt % args), flush=True)

    def send_json(self, status, obj):
        payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def authorized(self):
        auth = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not auth.startswith(prefix):
            return False
        supplied = auth[len(prefix):].strip().encode()
        expected = self.server.ingest_token.encode()
        return hmac.compare_digest(supplied, expected)

    def do_HEAD(self):
        if urlparse(self.path).path == "/health":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.send_response(HTTPStatus.NOT_FOUND)
            self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            with connect(self.server.db_path) as db:
                scans = db.execute("SELECT COUNT(*) AS n FROM scans").fetchone()["n"]
                observations = db.execute("SELECT COUNT(*) AS n FROM observations").fetchone()["n"]
                last = db.execute("SELECT MAX(received_at_ms) AS t FROM scans").fetchone()["t"]
            self.send_json(HTTPStatus.OK, {"ok": True, "scans": scans, "observations": observations, "last_received_at_ms": last})
            return
        if parsed.path == "/api/v1/recent":
            if not self.authorized():
                self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            qs = parse_qs(parsed.query)
            limit = max(1, min(500, safe_int((qs.get("limit") or [100])[0]) or 100))
            with connect(self.server.db_path) as db:
                rows = db.execute(
                    """SELECT id,scan_uuid,device_id,captured_at_ms,received_at_ms,latitude,longitude,accuracy_m,
                              connected_bssid,connected_ssid
                       FROM scans ORDER BY captured_at_ms DESC LIMIT ?""", (limit,)
                ).fetchall()
            self.send_json(HTTPStatus.OK, {"items": [dict(r) for r in rows]})
            return
        self.send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path not in ("/api/v1/scans", "/api/v1/scans/batch"):
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        if not self.authorized():
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self.send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "invalid_body_size"})
            return
        try:
            body = self.rfile.read(length)
            payload = json.loads(body.decode("utf-8"))
            items = payload if isinstance(payload, list) else [payload]
            if len(items) > 100:
                raise ValueError("batch too large")
            accepted = duplicates = observations = 0
            location_policy = {
                "gps_required": True,
                "reason": "no_fresh_policy",
                "confidence": 0.0,
                "verify_after_ms": 60000,
            }
            with connect(self.server.db_path) as db:
                db.execute("BEGIN IMMEDIATE")
                for item in items:
                    created, obs, scan_id = insert_scan(db, item)
                    if created:
                        accepted += 1
                        observations += obs
                    else:
                        duplicates += 1
                    # Classify every scan, including historical queue replays, so learning catches
                    # up after connectivity loss. The last item policy is what the client applies.
                    location_policy = classify_scan(db, scan_id)
                db.commit()
            self.send_json(HTTPStatus.OK, {
                "ok": True,
                "accepted": accepted,
                "duplicates": duplicates,
                "observations": observations,
                "location_policy": location_policy,
            })
        except (ValueError, json.JSONDecodeError) as e:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "bad_request", "detail": str(e)[:300]})
        except Exception as e:
            print("ingest error:", repr(e), flush=True)
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal_error"})


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("--bind", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8795)
    args = p.parse_args()
    cfg = load_config(Path(args.config))
    db_path = Path(args.db)
    init_db(db_path)
    httpd = ThreadingHTTPServer((args.bind, args.port), Handler)
    httpd.ingest_token = cfg["ingest_token"]
    httpd.db_path = db_path
    print(f"wifi-location API listening on {args.bind}:{args.port}; db={db_path}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
