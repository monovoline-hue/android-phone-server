package com.zonira.monitorbridge;

import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.os.BatteryManager;
import android.os.PowerManager;

import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

/**
 * Reads battery + display state on demand via official public APIs only.
 *
 * Battery: the ACTION_BATTERY_CHANGED sticky broadcast - registering with a
 * null receiver returns the last sticky intent synchronously. No polling, no
 * wakelock, no DUMP permission.
 *
 * Display: PowerManager.isInteractive() - true means the screen is on and
 * user-interactive.
 *
 * Every field degrades to JSON null when the platform does not supply a
 * plausible value. Nothing is invented: no 0%, no 0.0 C, no UNKNOWN enums
 * where the framework clearly gave nothing.
 */
public final class DeviceStatus {

    private DeviceStatus() {}

    /** @return the full /status JSON document. */
    public static String json(Context ctx) {
        StringBuilder b = new StringBuilder(512);
        b.append("{\"schema\":\"zonira-monitor-bridge/v1\"");
        b.append(",\"timestamp\":\"").append(now()).append("\"");
        b.append(",\"battery\":").append(batteryJson(ctx));
        b.append(",\"display\":").append(displayJson(ctx));
        b.append("}");
        return b.toString();
    }

    // ------------------------------------------------------------------
    // Battery
    // ------------------------------------------------------------------
    private static String batteryJson(Context ctx) {
        Intent i = null;
        try {
            i = ctx.registerReceiver(null, new IntentFilter(Intent.ACTION_BATTERY_CHANGED));
        } catch (Throwable t) {
            // fall through: every field becomes null
        }
        if (i == null) {
            return "{\"percentage\":null,\"status\":null,\"charging\":null,\"plugged\":null,"
                    + "\"health\":null,\"temperature_c\":null,\"voltage_mv\":null}";
        }

        int level = i.getIntExtra(BatteryManager.EXTRA_LEVEL, -1);
        int scale = i.getIntExtra(BatteryManager.EXTRA_SCALE, -1);
        int status = i.getIntExtra(BatteryManager.EXTRA_STATUS, -1);
        int plugged = i.getIntExtra(BatteryManager.EXTRA_PLUGGED, -1);
        int health = i.getIntExtra(BatteryManager.EXTRA_HEALTH, -1);
        int temp = i.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, -1);
        int volt = i.getIntExtra(BatteryManager.EXTRA_VOLTAGE, -1);

        // percentage only when both parts are valid and coherent
        Integer pct = null;
        if (scale > 0 && level >= 0 && level <= scale) {
            pct = (level * 100) / scale;
        }

        Boolean charging = null;
        if (status != -1) {
            charging = (status == BatteryManager.BATTERY_STATUS_CHARGING
                     || status == BatteryManager.BATTERY_STATUS_FULL);
        }

        Double tempC = null;
        // raw is tenths of a degree C; -1 means the extra was absent.
        // plausibility window: -20.0 C .. 80.0 C
        if (temp != -1 && temp >= -200 && temp <= 800) {
            tempC = temp / 10.0;
        }

        Integer voltMv = null;
        // real Li-ion cell: 2500..5000 mV; anything else is not a voltage
        if (volt >= 2500 && volt <= 5000) {
            voltMv = volt;
        }

        StringBuilder b = new StringBuilder(320);
        b.append("{\"percentage\":").append(pct == null ? "null" : pct);
        b.append(",\"status\":").append(q(status == -1 ? null : statusName(status)));
        b.append(",\"charging\":").append(charging == null ? "null" : charging);
        b.append(",\"plugged\":").append(q(plugged == -1 ? null : pluggedName(plugged)));
        b.append(",\"health\":").append(q(health == -1 ? null : healthName(health)));
        b.append(",\"temperature_c\":").append(tempC == null ? "null" : tempC);
        b.append(",\"voltage_mv\":").append(voltMv == null ? "null" : voltMv);
        b.append("}");
        return b.toString();
    }

    private static String statusName(int s) {
        switch (s) {
            case BatteryManager.BATTERY_STATUS_UNKNOWN:     return "UNKNOWN";
            case BatteryManager.BATTERY_STATUS_CHARGING:    return "CHARGING";
            case BatteryManager.BATTERY_STATUS_DISCHARGING: return "DISCHARGING";
            case BatteryManager.BATTERY_STATUS_NOT_CHARGING:return "NOT_CHARGING";
            case BatteryManager.BATTERY_STATUS_FULL:        return "FULL";
            default: return "UNKNOWN(" + s + ")"; // honest: report the raw code
        }
    }

    private static String pluggedName(int p) {
        switch (p) {
            case 0:  return "UNPLUGGED";
            case 1:  return "AC";
            case 2:  return "USB";
            case 4:  return "WIRELESS";
            case 8:  return "DOCK";
            default: return "UNKNOWN(" + p + ")";
        }
    }

    private static String healthName(int h) {
        switch (h) {
            case BatteryManager.BATTERY_HEALTH_UNKNOWN:        return "UNKNOWN";
            case BatteryManager.BATTERY_HEALTH_GOOD:           return "GOOD";
            case BatteryManager.BATTERY_HEALTH_OVERHEAT:       return "OVERHEAT";
            case BatteryManager.BATTERY_HEALTH_DEAD:           return "DEAD";
            case BatteryManager.BATTERY_HEALTH_OVER_VOLTAGE:   return "OVER_VOLTAGE";
            case BatteryManager.BATTERY_HEALTH_UNSPECIFIED_FAILURE: return "FAILURE";
            case BatteryManager.BATTERY_HEALTH_COLD:           return "COLD";
            default: return "UNKNOWN(" + h + ")";
        }
    }

    // ------------------------------------------------------------------
    // Display
    // ------------------------------------------------------------------
    private static String displayJson(Context ctx) {
        Boolean interactive = null;
        try {
            PowerManager pm = (PowerManager) ctx.getSystemService(Context.POWER_SERVICE);
            if (pm != null) {
                interactive = pm.isInteractive();
            }
        } catch (Throwable t) {
            // stay null
        }
        String state = null;
        if (interactive != null) {
            state = interactive ? "ON" : "OFF";
        }
        return "{\"interactive\":" + (interactive == null ? "null" : interactive)
                + ",\"state\":" + q(state) + "}";
    }

    // ------------------------------------------------------------------
    // helpers
    // ------------------------------------------------------------------
    private static String q(String s) {
        return s == null ? "null" : "\"" + s + "\"";
    }

    private static String now() {
        SimpleDateFormat f = new SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ssXXX", Locale.US);
        return f.format(new Date());
    }
}
