package net.wigle.wigleandroid.listener;

import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;
import android.database.sqlite.SQLiteOpenHelper;
import android.location.Location;
import android.net.wifi.ScanResult;
import android.net.wifi.WifiInfo;
import android.net.wifi.WifiManager;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.provider.Settings;

import net.wigle.wigleandroid.BuildConfig;
import net.wigle.wigleandroid.MainActivity;
import net.wigle.wigleandroid.util.Logging;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;

/**
 * Private location fingerprint uploader for the SHARTEMAN fork.
 *
 * WiGLE keeps its normal local DB. This sidecar stores complete Wi-Fi scan batches in
 * a second app-private SQLite queue, including the location attached to that scan. The
 * queue survives lost connectivity and app restarts. Uploads are idempotent because each
 * scan has a UUID and the server enforces uniqueness.
 */
public final class FingerprintUploader {
    private static final long MIN_SAMPLE_INTERVAL_MS = 30_000L;
    private static final int MAX_QUEUED_SCANS = 5_000;
    private static final int BATCH_SIZE = 20;
    private static final MediaType JSON = MediaType.get("application/json; charset=utf-8");

    private static volatile FingerprintUploader instance;
    private static volatile long locationSuppressedUntilElapsed = 0L;
    private static volatile String locationPolicyReason = "startup";

    private final Context context;
    private final QueueDb db;
    private final OkHttpClient client;
    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private final AtomicBoolean flushScheduled = new AtomicBoolean(false);
    private final String deviceId;
    private long lastSampleElapsed = Long.MIN_VALUE;

    private FingerprintUploader(final Context context) {
        this.context = context.getApplicationContext();
        this.db = new QueueDb(this.context);
        this.client = new OkHttpClient.Builder()
                .connectTimeout(15, TimeUnit.SECONDS)
                .readTimeout(20, TimeUnit.SECONDS)
                .writeTimeout(20, TimeUnit.SECONDS)
                .build();
        this.deviceId = stableDeviceId(this.context);
        scheduleFlush();
    }

    public static FingerprintUploader get(final Context context) {
        if (instance == null) {
            synchronized (FingerprintUploader.class) {
                if (instance == null) {
                    instance = new FingerprintUploader(context);
                }
            }
        }
        return instance;
    }

    public static boolean isLocationSuppressed() {
        return SystemClock.elapsedRealtime() < locationSuppressedUntilElapsed;
    }

    public static String getLocationPolicyReason() {
        return locationPolicyReason;
    }

    private static void applyLocationPolicy(final boolean gpsRequired,
                                            final long verifyAfterMs,
                                            final String reason) {
        final long now = SystemClock.elapsedRealtime();
        if (gpsRequired) {
            locationSuppressedUntilElapsed = 0L;
            locationPolicyReason = reason == null ? "gps_required" : reason;
        } else {
            // Never allow a remote policy to suppress Android location indefinitely. A bounded
            // verification window makes the client fail-safe toward data quality if the server
            // disappears or a learned fingerprint becomes stale.
            final long bounded = Math.max(60_000L, Math.min(verifyAfterMs, 60L * 60L * 1000L));
            locationSuppressedUntilElapsed = now + bounded;
            locationPolicyReason = reason == null ? "known_wifi_fingerprint" : reason;
        }

        new Handler(Looper.getMainLooper()).post(() -> {
            final MainActivity activity = MainActivity.getMainActivity();
            if (activity != null && activity.isScanning()) {
                if (gpsRequired) {
                    activity.setLocationUpdates();
                } else {
                    activity.setLocationUpdates(0L, 0f);
                }
            }
        });
    }

    public synchronized void enqueue(final Location location,
                                     final List<ScanResult> results,
                                     final WifiManager wifiManager) {
        if (!enabled() || results == null || results.isEmpty()) {
            return;
        }
        final long elapsed = SystemClock.elapsedRealtime();
        if (lastSampleElapsed != Long.MIN_VALUE && elapsed - lastSampleElapsed < MIN_SAMPLE_INTERVAL_MS) {
            return;
        }
        lastSampleElapsed = elapsed;

        try {
            final JSONObject payload = buildPayload(location, results, wifiManager);
            final SQLiteDatabase writable = db.getWritableDatabase();
            final ContentValues values = new ContentValues();
            values.put("created_at_ms", System.currentTimeMillis());
            values.put("payload", payload.toString());
            values.put("attempts", 0);
            writable.insertOrThrow("upload_queue", null, values);
            trimQueue(writable);
            scheduleFlush();
        } catch (final Exception e) {
            Logging.error("fingerprint enqueue failed: " + e, e);
        }
    }

    private boolean enabled() {
        return BuildConfig.FINGERPRINT_ENDPOINT != null
                && !BuildConfig.FINGERPRINT_ENDPOINT.trim().isEmpty();
    }

    private JSONObject buildPayload(final Location location,
                                    final List<ScanResult> results,
                                    final WifiManager wifiManager) throws JSONException {
        final long capturedAt = System.currentTimeMillis();
        final long wallMinusElapsed = capturedAt - SystemClock.elapsedRealtime();
        final JSONObject root = new JSONObject();
        root.put("scan_uuid", UUID.randomUUID().toString());
        root.put("device_id", deviceId);
        root.put("captured_at_ms", capturedAt);
        root.put("source", "wigle-private-fingerprint");
        root.put("app_version", BuildConfig.VERSION_NAME);

        if (location != null) {
            final JSONObject loc = new JSONObject();
            loc.put("latitude", location.getLatitude());
            loc.put("longitude", location.getLongitude());
            loc.put("accuracy_m", location.hasAccuracy() ? location.getAccuracy() : JSONObject.NULL);
            loc.put("altitude_m", location.hasAltitude() ? location.getAltitude() : JSONObject.NULL);
            loc.put("time_ms", location.getTime());
            root.put("location", loc);
        } else {
            root.put("location", new JSONObject());
        }

        String connectedBssid = null;
        String connectedSsid = null;
        try {
            final WifiInfo info = wifiManager == null ? null : wifiManager.getConnectionInfo();
            if (info != null) {
                connectedBssid = normalizeBssid(info.getBSSID());
                connectedSsid = cleanSsid(info.getSSID());
            }
        } catch (final SecurityException ignored) {
            // Fine: nearby scans are still useful when Android hides connection info.
        }
        if (connectedBssid != null) root.put("connected_bssid", connectedBssid);
        if (connectedSsid != null) root.put("connected_ssid", connectedSsid);

        final JSONArray wifi = new JSONArray();
        for (final ScanResult result : results) {
            if (result == null || result.BSSID == null) continue;
            final JSONObject row = new JSONObject();
            final String bssid = normalizeBssid(result.BSSID);
            row.put("bssid", bssid);
            row.put("ssid", result.SSID == null ? "" : result.SSID);
            row.put("rssi", result.level);
            row.put("frequency_mhz", result.frequency);
            row.put("capabilities", result.capabilities == null ? "" : result.capabilities);
            // ScanResult.timestamp is microseconds since boot. Convert it to wall-clock time so
            // the server can reject stale cached results instead of assuming they were seen now.
            final long seenAtMs = wallMinusElapsed + (result.timestamp / 1000L);
            row.put("seen_at_ms", seenAtMs);
            row.put("is_connected", connectedBssid != null && connectedBssid.equals(bssid));
            wifi.put(row);
        }
        root.put("wifi", wifi);
        return root;
    }

    private void trimQueue(final SQLiteDatabase writable) {
        try {
            writable.execSQL(
                    "DELETE FROM upload_queue WHERE id IN (SELECT id FROM upload_queue ORDER BY id DESC LIMIT -1 OFFSET ?)",
                    new Object[]{MAX_QUEUED_SCANS});
        } catch (final Exception e) {
            Logging.warn("fingerprint queue trim failed: " + e);
        }
    }

    /**
     * Retry any queued private uploads. Safe to call repeatedly from lifecycle/foreground-service
     * heartbeats; AtomicBoolean prevents concurrent flush loops.
     */
    public void kick() {
        scheduleFlush();
    }

    private void scheduleFlush() {
        if (!enabled() || !flushScheduled.compareAndSet(false, true)) {
            return;
        }
        executor.execute(() -> {
            try {
                flushLoop();
            } finally {
                flushScheduled.set(false);
            }
        });
    }

    private void flushLoop() {
        while (enabled()) {
            final Batch batch = readBatch();
            if (batch.ids.isEmpty()) return;
            if (!post(batch.payload)) {
                markFailed(batch.ids);
                return;
            }
            deleteBatch(batch.ids);
        }
    }

    private Batch readBatch() {
        final ArrayList<Long> ids = new ArrayList<>();
        final JSONArray payload = new JSONArray();
        final SQLiteDatabase readable = db.getReadableDatabase();
        try (Cursor c = readable.rawQuery(
                "SELECT id,payload FROM upload_queue ORDER BY id ASC LIMIT " + BATCH_SIZE, null)) {
            while (c.moveToNext()) {
                try {
                    ids.add(c.getLong(0));
                    payload.put(new JSONObject(c.getString(1)));
                } catch (final JSONException e) {
                    ids.add(c.getLong(0));
                    Logging.warn("dropping malformed fingerprint queue row " + c.getLong(0));
                }
            }
        }
        return new Batch(ids, payload);
    }

    private boolean post(final JSONArray payload) {
        if (payload.length() == 0) return true;
        final Request.Builder builder = new Request.Builder()
                .url(BuildConfig.FINGERPRINT_ENDPOINT)
                .header("X-Device-ID", deviceId)
                .header("User-Agent", "WiFiPlaces/" + BuildConfig.VERSION_NAME)
                .post(RequestBody.create(payload.toString(), JSON));
        if (BuildConfig.FINGERPRINT_TOKEN != null && BuildConfig.FINGERPRINT_TOKEN.length() >= 32) {
            builder.header("Authorization", "Bearer " + BuildConfig.FINGERPRINT_TOKEN);
        }
        final Request request = builder.build();
        try (Response response = client.newCall(request).execute()) {
            final boolean ok = response.isSuccessful();
            final String responseBody = response.body() == null ? "" : response.body().string();
            if (!ok) {
                Logging.warn("fingerprint upload HTTP " + response.code());
            } else {
                applyServerPolicyIfFresh(payload, responseBody);
            }
            return ok;
        } catch (final Exception e) {
            Logging.info("fingerprint upload deferred: " + e.getClass().getSimpleName() + ": " + e.getMessage());
            return false;
        }
    }

    private void applyServerPolicyIfFresh(final JSONArray payload, final String responseBody) {
        if (responseBody == null || responseBody.isEmpty() || payload == null || payload.length() == 0) {
            return;
        }
        try {
            final JSONObject newest = payload.optJSONObject(payload.length() - 1);
            if (newest == null) return;
            final long capturedAt = newest.optLong("captured_at_ms", 0L);
            // Never let replay of an old offline queue change today's power policy.
            if (capturedAt <= 0L || Math.abs(System.currentTimeMillis() - capturedAt) > 2L * 60L * 1000L) {
                return;
            }
            final JSONObject root = new JSONObject(responseBody);
            final JSONObject policy = root.optJSONObject("location_policy");
            if (policy == null) return;
            final boolean gpsRequired = policy.optBoolean("gps_required", true);
            final long verifyAfterMs = policy.optLong("verify_after_ms", 30L * 60L * 1000L);
            final String reason = policy.optString("reason", gpsRequired ? "gps_required" : "known_wifi_fingerprint");
            applyLocationPolicy(gpsRequired, verifyAfterMs, reason);
            Logging.info("smart location policy: gpsRequired=" + gpsRequired + " reason=" + reason
                    + " verifyAfterMs=" + verifyAfterMs);
        } catch (final Exception e) {
            Logging.warn("unable to parse smart location policy: " + e.getMessage());
        }
    }

    private void deleteBatch(final List<Long> ids) {
        if (ids.isEmpty()) return;
        final SQLiteDatabase writable = db.getWritableDatabase();
        writable.beginTransaction();
        try {
            for (final long id : ids) {
                writable.delete("upload_queue", "id=?", new String[]{Long.toString(id)});
            }
            writable.setTransactionSuccessful();
        } finally {
            writable.endTransaction();
        }
    }

    private void markFailed(final List<Long> ids) {
        if (ids.isEmpty()) return;
        final SQLiteDatabase writable = db.getWritableDatabase();
        writable.beginTransaction();
        try {
            for (final long id : ids) {
                writable.execSQL("UPDATE upload_queue SET attempts=attempts+1 WHERE id=?", new Object[]{id});
            }
            writable.setTransactionSuccessful();
        } finally {
            writable.endTransaction();
        }
    }

    private static String normalizeBssid(final String bssid) {
        if (bssid == null) return null;
        final String v = bssid.trim().toUpperCase(Locale.US);
        if (v.isEmpty() || "02:00:00:00:00:00".equals(v)) return null;
        return v;
    }

    private static String cleanSsid(final String ssid) {
        if (ssid == null || "<unknown ssid>".equalsIgnoreCase(ssid)) return null;
        String s = ssid.trim();
        if (s.length() >= 2 && s.startsWith("\"") && s.endsWith("\"")) {
            s = s.substring(1, s.length() - 1);
        }
        return s.isEmpty() ? null : s;
    }

    private static String stableDeviceId(final Context context) {
        try {
            final String raw = Settings.Secure.getString(context.getContentResolver(), Settings.Secure.ANDROID_ID);
            final MessageDigest digest = MessageDigest.getInstance("SHA-256");
            final byte[] hash = digest.digest((raw == null ? "unknown" : raw).getBytes(StandardCharsets.UTF_8));
            final StringBuilder out = new StringBuilder("android-");
            for (int i = 0; i < 8; i++) out.append(String.format(Locale.US, "%02x", hash[i]));
            return out.toString();
        } catch (final Exception e) {
            return "android-unknown";
        }
    }

    private static final class Batch {
        final ArrayList<Long> ids;
        final JSONArray payload;
        Batch(final ArrayList<Long> ids, final JSONArray payload) {
            this.ids = ids;
            this.payload = payload;
        }
    }

    private static final class QueueDb extends SQLiteOpenHelper {
        QueueDb(final Context context) {
            super(context, "wifi_places_sync.sqlite3", null, 1);
        }

        @Override
        public void onCreate(final SQLiteDatabase db) {
            db.execSQL("CREATE TABLE upload_queue (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at_ms INTEGER NOT NULL, payload TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0)");
            db.execSQL("CREATE INDEX idx_upload_queue_created ON upload_queue(created_at_ms)");
        }

        @Override
        public void onUpgrade(final SQLiteDatabase db, final int oldVersion, final int newVersion) {
            // v1 only
        }
    }
}
