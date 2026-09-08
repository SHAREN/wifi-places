# WiFi Places fingerprint transport

WiFi Places keeps the upstream WiGLE scanner/database while adding a separate self-hosted fingerprint transport.

The sidecar uploader samples a complete Wi-Fi scan batch at a bounded cadence, preserves it in an app-private SQLite retry queue, and uploads it to a user-controlled HTTPS endpoint. A batch retains the current Android location when available plus BSSID, SSID, RSSI, frequency, capabilities, connection state, and Android scan timestamps.

Build-time private configuration can be supplied through ignored `private.properties` or Gradle project properties:

- `fingerprintEndpoint` — HTTPS batch-ingest endpoint;
- `fingerprintAuth` — private bearer token;
- `updateManifestUrl` — optional self-hosted APK update manifest.

None of those values belong in Git. The public source defaults them to empty strings.

The upload queue is idempotent through a per-scan UUID and survives connectivity loss and process restarts. The server can return a smart `location_policy`; a recent response may temporarily suppress continuous Android location updates when a learned Wi-Fi fingerprint identifies a place with high confidence.

See `docs/SMART_LOCATION_ARCHITECTURE.md` for the power-state and place-learning design and `server/` for the generic self-hosted receiver.
