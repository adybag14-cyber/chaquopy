package com.mobilefork.chaquopycompat;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import android.content.Context;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.nio.charset.StandardCharsets;
import org.json.JSONObject;
import org.junit.Test;
import org.junit.runner.RunWith;

/** A host build or an assumption skip must never satisfy this device gate. */
@RunWith(AndroidJUnit4.class)
public final class FullCompatibilityTest {
    @Test
    public void fullCompatibilityMatrix() throws Exception {
        Context context = InstrumentationRegistry.getInstrumentation().getTargetContext();
        if (!Python.isStarted()) {
            Python.start(new AndroidPlatform(context));
        }
        Python python = Python.getInstance();
        File reportFile = new File(context.getFilesDir(), "runtime-smoke.json");
        PyObject arguments = python.getBuiltins().callAttr("list", (Object) new String[] {
                "--package", "full", "--require-android", "--json-output", reportFile.getAbsolutePath()
        });
        int result = python.getModule("runtime_smoke").callAttr("main", arguments).toInt();
        assertTrue("The Python suite must persist its runtime evidence", reportFile.isFile());
        ByteArrayOutputStream reportBytes = new ByteArrayOutputStream();
        try (FileInputStream input = new FileInputStream(reportFile)) {
            byte[] buffer = new byte[8192];
            int count;
            while ((count = input.read(buffer)) != -1) {
                reportBytes.write(buffer, 0, count);
            }
        }
        String reportText = new String(reportBytes.toByteArray(), StandardCharsets.UTF_8);
        JSONObject report = new JSONObject(reportText);
        assertEquals("Python compatibility failure: " + reportText, 0, result);
        assertEquals("passed", report.getString("status"));
        assertTrue("A host-only result is not acceptable", report.getBoolean("android_execution_verified"));
        assertTrue("No tests executed", report.getJSONObject("tests").getInt("run") > 0);
        assertEquals("Required tests may not be skipped", 0,
                report.getJSONObject("tests").getJSONArray("skipped").length());
    }
}
