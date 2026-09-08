# Self-hosted fingerprint server

This directory contains the generic private ingestion server used by the WiFi Places fork.

It intentionally does not contain any personal hostname, location data, learned fingerprints, or real authentication secret.

## Run

1. Copy `config.example.json` to `config.json`.
2. Generate a strong random ingest secret and put it in `config.json`.
3. Start:

```bash
python3 server.py --config ./config.json --db ./data/wifi_location.sqlite3 --bind 127.0.0.1 --port 8795
```

Put the service behind HTTPS before exposing it outside a trusted LAN.

The Android private build is configured with Gradle properties:

```text
-PfingerprintEndpoint=https://example.invalid/api/v1/scans/batch
-PfingerprintAuth=[REDACTED_SECRET]
```

Do not commit those values.

## Smart place learning

`smart_location.py` learns automatic places only after repeated location-bearing scans in a spatial cluster. It then builds a stable BSSID fingerprint and can recognize later scans without a current GPS fix.

The ingestion response includes `location_policy`. A compatible Android client can temporarily stop continuous location updates after a high-confidence Wi-Fi match and re-enable location when the fingerprint is new, ambiguous, changed, or due for verification.

The current thresholds are deliberately conservative and are expected to evolve with real-world data. See `../docs/SMART_LOCATION_ARCHITECTURE.md`.

## Raw data vs Home Assistant

Keep raw scan observations in this database. Do not mirror full BSSID lists into Home Assistant Recorder. Export only compact state such as inferred place, confidence, inference source, collector health, and last upload time.
