package com.zonira.monitorbridge;

import android.app.Application;
import android.content.Context;

/** Process-wide Context so the HTTP thread can read system services. */
public class AppContextHolder extends Application {

    private static Context app;

    @Override
    public void onCreate() {
        super.onCreate();
        app = this;
    }

    public static Context get() {
        return app;
    }
}
