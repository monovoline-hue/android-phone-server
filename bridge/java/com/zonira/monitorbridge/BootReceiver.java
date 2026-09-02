package com.zonira.monitorbridge;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** Starts the bridge service after boot. No logic, no work, no wakelock. */
public class BootReceiver extends BroadcastReceiver {

    @Override
    public void onReceive(Context context, Intent intent) {
        String a = intent == null ? null : intent.getAction();
        if (a == null) {
            return;
        }
        if (a.equals(Intent.ACTION_BOOT_COMPLETED)
                || a.equals(Intent.ACTION_LOCKED_BOOT_COMPLETED)
                || a.equals("android.intent.action.QUICKBOOT_POWERON")) {
            Intent svc = new Intent(context, BridgeService.class);
            context.startForegroundService(svc);
        }
    }
}
