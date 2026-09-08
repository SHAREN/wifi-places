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

The configured ingestion endpoint itself is not treated as a secret. A generic build can leave the endpoint empty; the owner's locally built APK injects it through ignored `private.properties`.

## Release assets

Each GitHub Release contains:

- `WiFi-Places-Logger-<tag>.apk`;
- `update.json` with the version code, version name, GitHub APK URL, and SHA-256.

The Android signing key remains only on the owner's development machine and is backed up separately. It must never be committed or uploaded to the public repository. Keeping the same signing certificate is required for Android to install new versions over the existing package without uninstalling it.

## Release procedure

1. Update Android `versionCode` and `versionName`.
2. Build locally using the preserved signing key and ignored `private.properties`.
3. Verify that the APK contains no bearer token and points its updater to GitHub Releases.
4. Commit and push the source to `SHAREN/wifi-places`.
5. Create a GitHub Release and upload the APK plus generated `update.json`.
6. Existing installations discover the new release automatically and download the APK from GitHub.

This keeps the public repository and release APK free of the private server bearer token while still making GitHub the actual download CDN for both manual installs and automatic updates.
