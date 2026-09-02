package com.zonira.monitorbridge;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.view.Gravity;
import android.widget.TextView;

/**
 * Only a manual launcher / status screen for debugging. The unattended flow
 * never needs it: BootReceiver starts the service after boot.
 */
public class MainActivity extends Activity {

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        startService(new Intent(this, BridgeService.class));

        TextView v = new TextView(this);
        v.setGravity(Gravity.CENTER);
        v.setTextSize(16);
        v.setText("ZONIRA Monitor Bridge\n\n"
                + "GET http://127.0.0.1:8765/status\n\n"
                + "server alive: " + (AppContextHolder.get() != null));
        setContentView(v);
    }
}
