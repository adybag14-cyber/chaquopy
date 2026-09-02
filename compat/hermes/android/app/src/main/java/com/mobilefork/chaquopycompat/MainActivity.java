package com.mobilefork.chaquopycompat;

import android.app.Activity;
import android.os.Bundle;
import android.widget.TextView;

/** Nonproduction launcher; the actual compatibility suite runs by instrumentation. */
public final class MainActivity extends Activity {
    @Override
    public void onCreate(Bundle state) {
        super.onCreate(state);
        TextView message = new TextView(this);
        int padding = Math.round(24 * getResources().getDisplayMetrics().density);
        message.setPadding(padding, padding, padding, padding);
        message.setTextSize(20);
        message.setText("Hermes / Chaquopy compatibility laboratory\n\n"
                + "This is not the Hermes app.\n\n"
                + "Run FullCompatibilityTest with Android instrumentation. "
                + "Opening this screen does not prove package compatibility. "
                + "Tests use loopback fixtures and no service credentials.");
        setContentView(message);
    }
}

