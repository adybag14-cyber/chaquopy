"""Guard the app lifecycle required by post-instrumentation evidence collection."""

from pathlib import Path
import unittest


ANDROID_ROOT = Path(__file__).resolve().parents[1]
KEEP_APKS = "android.injected.androidTest.leaveApksInstalledAfterRun"


class EvidenceCollectionContractTests(unittest.TestCase):
    def test_gradle_preserves_the_original_report_until_adb_collects_it(self):
        values = []
        for line in (ANDROID_ROOT / "gradle.properties").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith(("#", "!")):
                continue
            key, separator, value = line.partition("=")
            if separator and key.strip() == KEEP_APKS:
                values.append(value.strip())
        self.assertEqual(
            values, ["true"], "AGP must not delete private runtime evidence after tests"
        )


if __name__ == "__main__":
    unittest.main()
