# Smart Wi‑Fi Location Architecture

## Goal

The app should learn places from repeated nearby Wi‑Fi fingerprints and use GPS only when it materially improves confidence. Wi‑Fi scanning is the cheap always-on signal. Precise location is an expensive confirmation/learning signal.

The target behavior is:

1. Observe nearby Wi‑Fi BSSIDs continuously at a bounded cadence.
2. If the current fingerprint confidently matches a learned place, infer that place and suppress continuous GPS/network-location requests.
3. If the fingerprint is unknown, weak, materially changed, or ambiguous, temporarily enable location and collect enough samples to learn/repair the place fingerprint.
4. Once a place is stable, return to low-power Wi‑Fi-only recognition.
5. Periodically re-verify learned places so stale fingerprints do not remain trusted forever.
6. Never require connection to an observed Wi‑Fi network; visibility is enough.

## What counts as a place

A place is not created from one scan or one access point. A place becomes learnable only after repeated observations near the same physical location.

Initial conservative thresholds:

- at least 6 location-bearing scans in the same spatial cluster;
- observations spanning at least a few minutes, rather than a single instantaneous burst;
- preferably at least 3 stable BSSIDs in the learned fingerprint;
- BSSID is the primary identity; SSID is only a human-readable label;
- generic/mobile/transient APs should receive lower weight;
- a fingerprint is updated over time instead of being treated as immutable.

A server-side place can initially have an automatic name such as `auto-<id>`. A user or higher-level assistant can later assign a semantic name such as Home, Work, gym, cafe, or a person's home.

## Recognition score

Recognition should combine several signals rather than one AP:

- overlap of currently visible stable BSSIDs with the learned fingerprint;
- weighted Jaccard-style similarity;
- median RSSI compatibility (soft signal only; RSSI varies significantly indoors);
- GPS/network-location proximity when available;
- confidence gap between the best and second-best candidate;
- recency/stability of the stored fingerprint.

A confident match should require multiple stable BSSIDs. A single matching router is not enough for an automatic high-confidence place decision.

## Power state machine

### LEARNING / UNKNOWN

Use when:

- no known fingerprint matches;
- fewer than the minimum stable BSSID observations exist;
- two places have similar scores;
- the current fingerprint changed sharply;
- a periodic verification is due.

Behavior:

- keep Wi‑Fi scanning active;
- request Android location updates;
- prefer network/fused/network-provider location indoors when available;
- use GNSS when available for accurate confirmation;
- upload location-bearing samples until the cluster/fingerprint becomes stable.

### KNOWN / LOW_POWER

Use when a learned fingerprint matches with high confidence.

Behavior:

- keep Wi‑Fi scanning active;
- stop continuous location requests;
- infer the place/coordinates from the learned fingerprint on the server;
- continue sending scan batches without GPS;
- immediately leave this mode if similarity drops;
- force a periodic location verification after a bounded interval.

Initial verification target: about 30–60 minutes while the implementation is new. This can later be lengthened for very stable places.

### RECOVERY

Use when Wi‑Fi scan callbacks stop while scanning is logically enabled.

Behavior:

- detect missing callbacks with a watchdog;
- clear stale `scanInFlight` state;
- request a fresh scan without trying to toggle system Wi‑Fi on modern Android;
- retry with bounded backoff;
- record watchdog events for diagnostics;
- never require the user to manually pause/resume Scan for normal recovery.

## Server is the primary intelligence layer

The server owns the evolving place model because server-side logic can be improved without forcing an Android update.

The Android app sends timestamped scan batches and applies a small response policy, for example:

```json
{
  "location_policy": {
    "gps_required": false,
    "reason": "known_wifi_fingerprint",
    "confidence": 0.91,
    "place_id": 12,
    "verify_after_ms": 1800000
  }
}
```

If the server returns `gps_required=false`, the app suppresses continuous location updates until the verification deadline or until a later scan receives `gps_required=true`.

If the server is unreachable for too long, the app must fail safe toward data quality: eventually re-enable location rather than remaining permanently GPS-suppressed.

## Indoor location fallback

The upstream app supports Android's network location provider but defaults it off. This fork should default network-location fallback on because this project explicitly needs useful indoor position during learning.

The hierarchy is:

1. learned Wi‑Fi fingerprint for known places (lowest ongoing power);
2. Android network location for unknown/learning indoor places;
3. GNSS/GPS confirmation when available and useful.

The app should not hold GNSS continuously at a known place simply to re-prove a location already identified by a strong fingerprint.

## Data quality and staleness

For every Wi‑Fi observation retain Android `ScanResult.timestamp` and derive wall-clock `seen_at_ms`. Do not pretend stale cached scan results were observed at the current instant.

A server classifier should ignore or heavily down-weight observations that are too old relative to the scan capture time.

## Privacy and public repository rules

The public repository must never contain:

- a private ingestion token;
- a personal server hostname unless it is clearly an example placeholder;
- private location data;
- learned personal place fingerprints;
- exported local databases.

Self-hosted endpoint/token values are build-time or local runtime configuration and are ignored by Git.

Stock WiGLE.net upload controls are disabled in this fork by default because the purpose is private/self-hosted collection. Re-enabling third-party upload should require an explicit source change or clearly separate opt-in configuration.

## Updates

Desired behavior:

- app checks the project's GitHub Release manifest periodically;
- if a newer compatible APK exists, it can download it automatically while on a suitable network;
- standard Android normally still requires a user confirmation to install an APK unless the app is a privileged/device-owner installer;
- therefore the practical non-root target is: automatic check + automatic download + one Android installation confirmation;
- update signing key must remain stable so updates install over the existing package and retain its local queue/database.

For the owner's private build, releases may be produced from the same public source while injecting the private endpoint/token at build time. Secrets must never be placed in GitHub Release metadata or the public repository.

## Home Assistant / assistant integration

Raw scans remain outside Home Assistant Recorder. Home Assistant should receive compact state only, for example:

- current inferred place;
- confidence;
- inference source (`wifi`, `wifi+gps`, `gps`, `unknown`);
- last successful collector upload;
- collector health/watchdog status.

Daily analysis should work from compact visit/place summaries rather than feeding raw high-frequency Wi‑Fi rows into an LLM.

## Design invariant

The system is successful when, after a place has been learned, repeatedly visiting it requires almost no precise-location work: the surrounding Wi‑Fi fingerprint is enough to recognize the place, while GPS/location wakes only for learning, ambiguity, meaningful environmental change, and occasional verification.
