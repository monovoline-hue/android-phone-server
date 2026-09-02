package com.zonira.monitorbridge;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.os.IBinder;

/**
 * Minimal foreground service. It exists for exactly one reason: keep the
 * process alive so the loopback server keeps listening.
 *
 * - no WakeLock, no CPU waking, no polling, no timers
 * - the only work happens when a /status request arrives
 * - START_STICKY so Android revives it if the process is killed
 */
public class BridgeService extends Service {

    private static final String CHANNEL_ID = "bridge";
    private static final int NOTIF_ID = 1;

    private final StatusHttpServer server = new StatusHttpServer();

    @Override
    public void onCreate() {
        super.onCreate();
        createChannel();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        startForeground(NOTIF_ID, buildNotification());
        if (!server.isAlive()) {
            server.start();
        }
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        server.stop();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private void createChannel() {
        NotificationManager nm =
                (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm == null) {
            return;
        }
        NotificationChannel ch = new NotificationChannel(
                CHANNEL_ID, "Monitor Bridge", NotificationManager.IMPORTANCE_MIN);
        ch.setShowBadge(false);
        nm.createNotificationChannel(ch);
    }

    private Notification buildNotification() {
        Notification.Builder b;
        if (android.os.Build.VERSION.SDK_INT >= 26) {
            b = new Notification.Builder(this, CHANNEL_ID);
        } else {
            b = new Notification.Builder(this);
        }
        return b.setContentTitle("ZONIRA Monitor Bridge")
                .setContentText("127.0.0.1:8765/status")
                .setSmallIcon(R.drawable.ic_stat)
                .setOngoing(true)
                .build();
    }
}
