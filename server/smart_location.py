import math
import time

SMART_SCHEMA = """
CREATE TABLE IF NOT EXISTS scan_places (
    scan_id INTEGER PRIMARY KEY REFERENCES scans(id) ON DELETE CASCADE,
    place_id INTEGER NOT NULL REFERENCES places(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    confidence REAL NOT NULL,
    assigned_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scan_places_place ON scan_places(place_id);
"""

EARTH_RADIUS_M = 6371000.0
LEARN_RADIUS_M = 120.0
LEARN_WINDOW_MS = 20 * 60 * 1000
LEARN_MIN_SCANS = 6
LEARN_MIN_SPAN_MS = 4 * 60 * 1000
MAX_LEARNING_ACCURACY_M = 100.0
MIN_STABLE_BSSID_SCANS = 3
MIN_STABLE_BSSID_PROB = 0.30
MIN_RECOGNITION_OVERLAP = 3
MIN_RECOGNITION_SCORE = 0.68
MIN_BEST_GAP = 0.08
VERIFY_AFTER_MS = 30 * 60 * 1000
MAX_WIFI_AGE_MS = 2 * 60 * 1000


def init_smart_schema(db):
    db.executescript(SMART_SCHEMA)


def haversine_m(lat1, lon1, lat2, lon2):
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dlat = p2 - p1
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return EARTH_RADIUS_M * (2 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a))))


def _fresh_wifi_rows(db, scan_id):
    scan = db.execute("SELECT captured_at_ms FROM scans WHERE id=?", (scan_id,)).fetchone()
    if not scan:
        return []
    captured = int(scan["captured_at_ms"])
    rows = db.execute(
        "SELECT bssid,ssid,rssi,seen_at_ms FROM observations WHERE scan_id=?",
        (scan_id,),
    ).fetchall()
    out = []
    for row in rows:
        seen = row["seen_at_ms"]
        if seen is not None and abs(captured - int(seen)) > MAX_WIFI_AGE_MS:
            continue
        out.append(row)
    return out


def _place_maturity(db, place_id):
    total_scans = db.execute(
        "SELECT COUNT(*) AS n FROM scan_places WHERE place_id=?", (place_id,)
    ).fetchone()["n"]
    stable = db.execute(
        """SELECT COUNT(*) AS n FROM place_fingerprint
           WHERE place_id=? AND seen_scans>=? AND total_scans>=? AND weight>=?""",
        (place_id, MIN_STABLE_BSSID_SCANS, LEARN_MIN_SCANS, MIN_STABLE_BSSID_PROB),
    ).fetchone()["n"]
    return int(total_scans), int(stable)


def _assign_scan(db, scan_id, place_id, source, confidence):
    db.execute(
        """INSERT INTO scan_places(scan_id,place_id,source,confidence,assigned_at_ms)
           VALUES(?,?,?,?,?)
           ON CONFLICT(scan_id) DO UPDATE SET
             place_id=excluded.place_id,
             source=excluded.source,
             confidence=excluded.confidence,
             assigned_at_ms=excluded.assigned_at_ms""",
        (scan_id, place_id, source, float(confidence), int(time.time() * 1000)),
    )


def _refresh_centroid(db, place_id):
    rows = db.execute(
        """SELECT s.latitude,s.longitude,s.accuracy_m
           FROM scan_places sp JOIN scans s ON s.id=sp.scan_id
           WHERE sp.place_id=? AND s.latitude IS NOT NULL AND s.longitude IS NOT NULL
             AND (s.accuracy_m IS NULL OR s.accuracy_m<=?)
           ORDER BY s.captured_at_ms DESC LIMIT 200""",
        (place_id, MAX_LEARNING_ACCURACY_M),
    ).fetchall()
    if not rows:
        return
    weights = []
    for r in rows:
        acc = float(r["accuracy_m"] or 30.0)
        w = 1.0 / max(10.0, acc)
        weights.append((float(r["latitude"]), float(r["longitude"]), w))
    sw = sum(w for _, _, w in weights)
    lat = sum(lat * w for lat, _, w in weights) / sw
    lon = sum(lon * w for _, lon, w in weights) / sw
    db.execute(
        "UPDATE places SET latitude=?,longitude=?,updated_at_ms=? WHERE id=?",
        (lat, lon, int(time.time() * 1000), place_id),
    )


def _rebuild_fingerprint(db, place_id):
    rows = db.execute(
        """SELECT o.bssid,
                  COUNT(DISTINCT o.scan_id) AS seen_scans,
                  AVG(o.rssi) AS avg_rssi,
                  MAX(COALESCE(o.seen_at_ms,s.captured_at_ms)) AS last_seen_ms
           FROM scan_places sp
           JOIN scans s ON s.id=sp.scan_id
           JOIN observations o ON o.scan_id=s.id
           WHERE sp.place_id=?
             AND (o.seen_at_ms IS NULL OR ABS(s.captured_at_ms-o.seen_at_ms)<=?)
           GROUP BY o.bssid""",
        (place_id, MAX_WIFI_AGE_MS),
    ).fetchall()
    total = db.execute(
        "SELECT COUNT(*) AS n FROM scan_places WHERE place_id=?", (place_id,)
    ).fetchone()["n"]
    db.execute("DELETE FROM place_fingerprint WHERE place_id=?", (place_id,))
    if total <= 0:
        return
    for r in rows:
        seen = int(r["seen_scans"])
        prob = seen / float(total)
        # Weight stable APs strongly but never let one AP dominate recognition.
        weight = min(1.0, max(0.05, prob))
        db.execute(
            """INSERT INTO place_fingerprint
               (place_id,bssid,seen_scans,total_scans,avg_rssi,last_seen_ms,weight)
               VALUES(?,?,?,?,?,?,?)""",
            (place_id, r["bssid"], seen, int(total), r["avg_rssi"], r["last_seen_ms"], weight),
        )


def _nearest_place(db, lat, lon):
    rows = db.execute("SELECT id,latitude,longitude,radius_m FROM places WHERE latitude IS NOT NULL AND longitude IS NOT NULL").fetchall()
    best = None
    for r in rows:
        d = haversine_m(lat, lon, float(r["latitude"]), float(r["longitude"]))
        if best is None or d < best[0]:
            best = (d, r)
    return best


def _candidate_cluster(db, device_id, captured_at_ms, lat, lon):
    rows = db.execute(
        """SELECT id,captured_at_ms,latitude,longitude,accuracy_m
           FROM scans
           WHERE device_id=? AND captured_at_ms BETWEEN ? AND ?
             AND latitude IS NOT NULL AND longitude IS NOT NULL
             AND (accuracy_m IS NULL OR accuracy_m<=?)
           ORDER BY captured_at_ms""",
        (device_id, captured_at_ms - LEARN_WINDOW_MS, captured_at_ms, MAX_LEARNING_ACCURACY_M),
    ).fetchall()
    near = []
    for r in rows:
        if haversine_m(lat, lon, float(r["latitude"]), float(r["longitude"])) <= LEARN_RADIUS_M:
            near.append(r)
    return near


def _cluster_stable_bssid_count(db, cluster):
    """Return how many APs repeatedly appear across a candidate dwell cluster."""
    if not cluster:
        return 0
    scan_ids = [int(r["id"]) for r in cluster]
    placeholders = ",".join("?" for _ in scan_ids)
    rows = db.execute(
        f"""SELECT o.bssid,COUNT(DISTINCT o.scan_id) AS n
            FROM observations o JOIN scans s ON s.id=o.scan_id
            WHERE o.scan_id IN ({placeholders})
              AND (o.seen_at_ms IS NULL OR ABS(s.captured_at_ms-o.seen_at_ms)<=?)
            GROUP BY o.bssid""",
        (*scan_ids, MAX_WIFI_AGE_MS),
    ).fetchall()
    required = max(MIN_STABLE_BSSID_SCANS, math.ceil(len(scan_ids) * 0.40))
    return sum(1 for r in rows if int(r["n"]) >= required)


def _ensure_place_from_gps(db, scan_id):
    scan = db.execute(
        "SELECT id,device_id,captured_at_ms,latitude,longitude,accuracy_m FROM scans WHERE id=?",
        (scan_id,),
    ).fetchone()
    if not scan or scan["latitude"] is None or scan["longitude"] is None:
        return None
    accuracy = float(scan["accuracy_m"] or 9999.0)
    if accuracy > MAX_LEARNING_ACCURACY_M:
        return None
    lat = float(scan["latitude"])
    lon = float(scan["longitude"])
    nearest = _nearest_place(db, lat, lon)
    if nearest is not None:
        distance, place = nearest
        radius = max(LEARN_RADIUS_M, float(place["radius_m"] or LEARN_RADIUS_M))
        if distance <= radius:
            _assign_scan(db, scan_id, int(place["id"]), "gps_near_place", max(0.5, 1.0 - distance / max(radius, 1.0)))
            _refresh_centroid(db, int(place["id"]))
            _rebuild_fingerprint(db, int(place["id"]))
            return int(place["id"])

    cluster = _candidate_cluster(db, scan["device_id"], int(scan["captured_at_ms"]), lat, lon)
    if len(cluster) < LEARN_MIN_SCANS:
        return None
    span = int(cluster[-1]["captured_at_ms"]) - int(cluster[0]["captured_at_ms"])
    if span < LEARN_MIN_SPAN_MS:
        return None
    # A real place should expose a repeatable RF environment. This rejects many slow-movement,
    # traffic-light, and GPS-drift clusters before they become persistent places.
    if _cluster_stable_bssid_count(db, cluster) < MIN_RECOGNITION_OVERLAP:
        return None

    now = int(time.time() * 1000)
    slug = f"auto-{scan['device_id'][:16]}-{int(scan['captured_at_ms'])}"
    cur = db.execute(
        "INSERT INTO places(slug,name,latitude,longitude,radius_m,created_at_ms,updated_at_ms) VALUES(?,?,?,?,?,?,?)",
        (slug, "Auto place", lat, lon, LEARN_RADIUS_M, now, now),
    )
    place_id = int(cur.lastrowid)
    for r in cluster:
        _assign_scan(db, int(r["id"]), place_id, "gps_learning_cluster", 0.8)
    _refresh_centroid(db, place_id)
    _rebuild_fingerprint(db, place_id)
    return place_id


def _recognize_wifi(db, scan_id):
    current_rows = _fresh_wifi_rows(db, scan_id)
    current = {r["bssid"] for r in current_rows if r["bssid"]}
    if len(current) < MIN_RECOGNITION_OVERLAP:
        return None

    places = db.execute("SELECT id,latitude,longitude FROM places").fetchall()
    scored = []
    for place in places:
        fp = db.execute(
            """SELECT bssid,weight,seen_scans,total_scans FROM place_fingerprint
               WHERE place_id=? AND seen_scans>=? AND total_scans>=? AND weight>=?""",
            (place["id"], MIN_STABLE_BSSID_SCANS, LEARN_MIN_SCANS, MIN_STABLE_BSSID_PROB),
        ).fetchall()
        if not fp:
            continue
        total_weight = sum(float(r["weight"]) for r in fp)
        matched = [r for r in fp if r["bssid"] in current]
        overlap = len(matched)
        if overlap < MIN_RECOGNITION_OVERLAP or total_weight <= 0:
            continue
        recall = sum(float(r["weight"]) for r in matched) / total_weight
        overlap_bonus = min(1.0, overlap / 5.0)
        score = 0.78 * recall + 0.22 * overlap_bonus
        scored.append((score, overlap, int(place["id"])))

    if not scored:
        return None
    scored.sort(reverse=True)
    best = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0.0
    if best[0] < MIN_RECOGNITION_SCORE or best[0] - second_score < MIN_BEST_GAP:
        return None
    return {"place_id": best[2], "confidence": min(0.99, best[0]), "overlap": best[1], "score": best[0]}


def classify_scan(db, scan_id):
    """Assign a scan when possible and return a client location-power policy."""
    init_smart_schema(db)
    scan = db.execute(
        "SELECT id,latitude,longitude,accuracy_m FROM scans WHERE id=?", (scan_id,)
    ).fetchone()
    if not scan:
        return {"gps_required": True, "reason": "scan_missing", "confidence": 0.0, "verify_after_ms": 60_000}

    place_id = _ensure_place_from_gps(db, scan_id)
    if place_id is not None:
        total_scans, stable = _place_maturity(db, place_id)
        if total_scans >= LEARN_MIN_SCANS and stable >= MIN_RECOGNITION_OVERLAP:
            return {
                "gps_required": False,
                "reason": "learned_place_verified",
                "confidence": min(0.95, 0.70 + min(stable, 8) * 0.03),
                "place_id": place_id,
                "verify_after_ms": VERIFY_AFTER_MS,
            }

    wifi_match = _recognize_wifi(db, scan_id)
    if wifi_match is not None:
        _assign_scan(db, scan_id, wifi_match["place_id"], "wifi_fingerprint", wifi_match["confidence"])
        # Keep fingerprints adaptive, but centroid changes only from scans that actually have location.
        _rebuild_fingerprint(db, wifi_match["place_id"])
        return {
            "gps_required": False,
            "reason": "known_wifi_fingerprint",
            "confidence": wifi_match["confidence"],
            "place_id": wifi_match["place_id"],
            "verify_after_ms": VERIFY_AFTER_MS,
            "overlap": wifi_match["overlap"],
        }

    return {
        "gps_required": True,
        "reason": "unknown_or_ambiguous_fingerprint",
        "confidence": 0.0,
        "verify_after_ms": 60_000,
    }
