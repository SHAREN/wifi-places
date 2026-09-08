package net.wigle.wigleandroid.util;

import android.app.DownloadManager;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Environment;
import android.provider.Settings;

import net.wigle.wigleandroid.BuildConfig;

import org.json.JSONObject;

import java.io.FileInputStream;
import java.io.InputStream;
import java.security.MessageDigest;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;

/**
 * Small self-hosted updater for private WiFi Places builds.
 *
 * The public source tree contains no private endpoint/token. A private build may inject
 * UPDATE_MANIFEST_URL. The manifest points to an APK produced from the same source tree and signed
 * with the same key. Android still requires the user to approve installation for a non-privileged
 * app; everything before that confirmation is automatic.
 */
public final class PrivateUpdateManager {
    private static final String PREFS = "wifi_places_update";
    private static final String PREF_LAST_CHECK = "last_check_ms";
    private static final String PREF_PENDING_VERSION = "pending_version";
    private static final String PREF_PENDING_DOWNLOAD_ID = "pending_download_id";
    private static final String PREF_PENDING_SHA256 = "pending_sha256";
    private static final String PREF_INSTALL_PERMISSION_PROMPTED_MS = "install_permission_prompted_ms";
    private static final long CHECK_INTERVAL_MS = 6L * 60L * 60L * 1000L;
    private static final long INSTALL_PERMISSION_REPROMPT_MS = 5L * 60L * 1000L;

    private static volatile PrivateUpdateManager instance;

    private final Context context;
    private final SharedPreferences prefs;
    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private final AtomicBoolean checking = new AtomicBoolean(false);
    private final OkHttpClient client = new OkHttpClient.Builder()
            .connectTimeout(15, TimeUnit.SECONDS)
            .readTimeout(30, TimeUnit.SECONDS)
            .build();

    private PrivateUpdateManager(final Context context) {
        this.context = context.getApplicationContext();
        this.prefs = this.context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        registerDownloadReceiver();
    }

    public static PrivateUpdateManager get(final Context context) {
        if (instance == null) {
            synchronized (PrivateUpdateManager.class) {
                if (instance == null) instance = new PrivateUpdateManager(context);
            }
        }
        return instance;
    }

    public void checkForUpdates(final boolean force) {
        // First resume a completed/pending download from a previous process lifetime. This also
        // handles the common flow where Android sent the user to "Install unknown apps" settings.
        final long pendingId = prefs.getLong(PREF_PENDING_DOWNLOAD_ID, -1L);
        if (pendingId > 0L) {
            executor.execute(() -> handleCompletedDownload(pendingId));
        }

        final String manifestUrl = BuildConfig.UPDATE_MANIFEST_URL;
        if (manifestUrl == null || manifestUrl.trim().isEmpty()) return;
        final long now = System.currentTimeMillis();
        final long last = prefs.getLong(PREF_LAST_CHECK, 0L);
        if (!force && now - last < CHECK_INTERVAL_MS) return;
        if (!checking.compareAndSet(false, true)) return;
        prefs.edit().putLong(PREF_LAST_CHECK, now).apply();
        executor.execute(() -> {
            try {
                checkNow(manifestUrl);
            } finally {
                checking.set(false);
            }
        });
    }

    private void checkNow(final String manifestUrl) {
        final Request request = new Request.Builder()
                .url(manifestUrl)
                .header("User-Agent", "WiFiPlacesUpdater/" + BuildConfig.VERSION_NAME)
                .get()
                .build();
        try (Response response = client.newCall(request).execute()) {
            if (!response.isSuccessful() || response.body() == null) {
                Logging.info("update manifest HTTP " + response.code());
                return;
            }
            final JSONObject json = new JSONObject(response.body().string());
            final int versionCode = json.optInt("version_code", 0);
            final String apkUrl = json.optString("apk_url", "");
            final String sha256 = json.optString("sha256", "").toLowerCase(Locale.US);
            if (versionCode <= BuildConfig.VERSION_CODE || apkUrl.isEmpty() || sha256.length() != 64) {
                return;
            }
            final int pendingVersion = prefs.getInt(PREF_PENDING_VERSION, 0);
            final long pendingDownloadId = prefs.getLong(PREF_PENDING_DOWNLOAD_ID, -1L);
            if (pendingVersion >= versionCode && pendingDownloadId > 0L) return;
            enqueueDownload(versionCode, apkUrl, sha256);
        } catch (final Exception e) {
            Logging.info("update check deferred: " + e.getClass().getSimpleName() + ": " + e.getMessage());
        }
    }

    private void enqueueDownload(final int versionCode, final String apkUrl, final String sha256) {
        final DownloadManager dm = (DownloadManager) context.getSystemService(Context.DOWNLOAD_SERVICE);
        if (dm == null) return;
        final String fileName = "WiFi-Places-Logger-" + versionCode + ".apk";
        final DownloadManager.Request request = new DownloadManager.Request(Uri.parse(apkUrl))
                .setTitle("WiFi Places update")
                .setDescription("Downloading version " + versionCode)
                .setMimeType("application/vnd.android.package-archive")
                .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
                .setDestinationInExternalFilesDir(context, Environment.DIRECTORY_DOWNLOADS, fileName);
        final long pendingDownloadId = dm.enqueue(request);
        prefs.edit()
                .putInt(PREF_PENDING_VERSION, versionCode)
                .putLong(PREF_PENDING_DOWNLOAD_ID, pendingDownloadId)
                .putString(PREF_PENDING_SHA256, sha256)
                .apply();
        Logging.info("queued WiFi Places update version " + versionCode + " downloadId=" + pendingDownloadId);
    }

    private void registerDownloadReceiver() {
        final IntentFilter filter = new IntentFilter(DownloadManager.ACTION_DOWNLOAD_COMPLETE);
        final BroadcastReceiver receiver = new BroadcastReceiver() {
            @Override
            public void onReceive(final Context receiverContext, final Intent intent) {
                final long id = intent == null ? -1L : intent.getLongExtra(DownloadManager.EXTRA_DOWNLOAD_ID, -1L);
                final long expectedId = prefs.getLong(PREF_PENDING_DOWNLOAD_ID, -1L);
                if (id <= 0 || id != expectedId) return;
                executor.execute(() -> handleCompletedDownload(id));
            }
        };
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            context.registerReceiver(receiver, filter, Context.RECEIVER_NOT_EXPORTED);
        } else {
            context.registerReceiver(receiver, filter);
        }
    }

    private void handleCompletedDownload(final long id) {
        final DownloadManager dm = (DownloadManager) context.getSystemService(Context.DOWNLOAD_SERVICE);
        if (dm == null) return;
        try (android.database.Cursor c = dm.query(new DownloadManager.Query().setFilterById(id))) {
            if (c == null || !c.moveToFirst()) return;
            final int status = c.getInt(c.getColumnIndexOrThrow(DownloadManager.COLUMN_STATUS));
            if (status == DownloadManager.STATUS_FAILED) {
                clearPendingDownload();
                return;
            }
            if (status != DownloadManager.STATUS_SUCCESSFUL) return;
            final String pendingSha256 = prefs.getString(PREF_PENDING_SHA256, "");
            if (!verifySha256(dm, id, pendingSha256)) {
                Logging.error("downloaded update SHA-256 mismatch");
                clearPendingDownload();
                return;
            }
            final Uri contentUri = dm.getUriForDownloadedFile(id);
            if (contentUri == null) return;
            if (requestInstall(contentUri)) {
                clearPendingDownload();
            }
        } catch (final Exception e) {
            Logging.error("update install preparation failed: " + e, e);
        }
    }

    private boolean verifySha256(final DownloadManager dm, final long downloadId, final String expected) {
        if (expected == null || expected.length() != 64) return false;
        try (android.os.ParcelFileDescriptor pfd = dm.openDownloadedFile(downloadId);
             InputStream in = pfd == null ? null : new FileInputStream(pfd.getFileDescriptor())) {
            if (in == null) return false;
            final MessageDigest digest = MessageDigest.getInstance("SHA-256");
            final byte[] buf = new byte[64 * 1024];
            int n;
            while ((n = in.read(buf)) > 0) digest.update(buf, 0, n);
            final StringBuilder actual = new StringBuilder(64);
            for (byte b : digest.digest()) actual.append(String.format(Locale.US, "%02x", b));
            return expected.equalsIgnoreCase(actual.toString());
        } catch (final Exception e) {
            Logging.error("update hash verification failed: " + e, e);
            return false;
        }
    }

    private void clearPendingDownload() {
        prefs.edit()
                .remove(PREF_PENDING_DOWNLOAD_ID)
                .remove(PREF_PENDING_SHA256)
                .apply();
    }

    private boolean requestInstall(final Uri apkUri) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            final PackageManager pm = context.getPackageManager();
            if (!pm.canRequestPackageInstalls()) {
                final long now = System.currentTimeMillis();
                final long lastPrompt = prefs.getLong(PREF_INSTALL_PERMISSION_PROMPTED_MS, 0L);
                if (now - lastPrompt >= INSTALL_PERMISSION_REPROMPT_MS) {
                    prefs.edit().putLong(PREF_INSTALL_PERMISSION_PROMPTED_MS, now).apply();
                    final Intent settings = new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                            Uri.parse("package:" + context.getPackageName()));
                    settings.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                    context.startActivity(settings);
                }
                // Keep the completed download registered. The guard thread will retry after the
                // user grants permission and then Android will show its normal install confirmation.
                return false;
            }
        }
        final Intent install = new Intent(Intent.ACTION_VIEW)
                .setDataAndType(apkUri, "application/vnd.android.package-archive")
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_GRANT_READ_URI_PERMISSION);
        context.startActivity(install);
        return true;
    }
}
