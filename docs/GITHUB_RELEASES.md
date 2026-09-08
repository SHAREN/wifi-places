# GitHub Releases and automatic updates

WiFi Places uses GitHub Releases as the APK distribution channel.

## Client update flow

Owner builds are compiled with:

- a self-hosted ingestion endpoint;
- no bearer ingest token;
- `updateManifestUrl=https://github.com/SHAREN/wifi-places/releases/latest/download/update.json`.

The Android updater periodically downloads `update.json` from the latest GitHub Release. When `version_code` is newer than the installed build it downloads the APK directly from the GitHub Release asset, verifies its SHA-256, and asks Android to install it. A normal non-root Android device still shows the operating system's final installation confirmation.

## Why the public APK contains no server token

A public GitHub Release must never contain the private ingestion bearer token. The collector sends its locally derived `X-Device-ID`; the server accepts only explicitly allow-listed device IDs and rejects unknown clients before reading their request body. The private bearer token remains available for administrative/API use but is not required by the owner's release APK.

The configured ingestion endpoint itself is not treated as a secret. A generic build can leave the endpoint empty; the owner's release workflow injects it from the GitHub repository variable `WIFI_PLACES_ENDPOINT`.

## Release automation

`.github/workflows/release.yml` builds on tags matching `v*` and creates a GitHub Release containing:

- `WiFi-Places-Logger-<tag>.apk`;
- `update.json` with the version code, version name, GitHub APK URL, and SHA-256.

The workflow requires:

- repository variable `WIFI_PLACES_ENDPOINT`;
- repository secret `WIFI_PLACES_DEBUG_KEYSTORE_B64` containing the base64-encoded signing keystore.

The signing key must remain unchanged because Android only installs an update over an existing package when the signing certificate matches. The key itself must never be committed to Git.

## Release procedure

1. Update Android `versionCode` and `versionName`.
2. Commit and push the source to the fork's default branch.
3. Create/push a tag such as `v0.5`.
4. GitHub Actions builds and publishes the release.
5. Existing installations discover the new `update.json` automatically and download the APK from GitHub.
