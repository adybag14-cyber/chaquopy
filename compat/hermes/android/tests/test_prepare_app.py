"""Offline tests of app staging, not wheel-content or Android-runtime proof."""

import json
from pathlib import Path
import tempfile
import unittest

from compat.hermes import prepare_app


class PrepareAppTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.wheelhouse = self.root / "wheels"
        self.wheelhouse.mkdir()
        self.app_root = self.root / "android"
        self.smoke = self.root / "runtime_smoke.py"
        self.smoke.write_text("def main(argv):\n    return 0\n", encoding="utf-8")

    def wheels(self, version="3.14", api=24):
        tag = "cp" + version.replace(".", "")
        for package, package_version in prepare_app.NATIVE_PACKAGES.items():
            for abi in prepare_app.ABIS:
                name = f"{package}-{package_version}-{tag}-{tag}-android_{api}_{abi}.whl"
                (self.wheelhouse / name).write_bytes(b"input inventory fixture, not a real wheel")
        (self.wheelhouse / "msgpack-1.2.2-py3-none-any.whl").write_bytes(b"pure wheel fixture")

    def prepare(self, version="3.14"):
        return prepare_app.prepare_app(self.wheelhouse, version, android_root=self.app_root,
                                       runtime_source=self.smoke)

    def test_records_input_inventory_and_exact_canonical_smoke(self):
        self.wheels()
        receipt = self.prepare()
        self.assertEqual(
            Path(receipt["generated_python_source"]).read_bytes(), self.smoke.read_bytes()
        )
        self.assertEqual(len(receipt["wheels"]), 5)
        self.assertFalse(receipt["android_compatibility_verified"])
        stored = json.loads((self.app_root / ".generated/preparation.json").read_text())
        self.assertEqual(stored, receipt)
        self.assertEqual(
            (self.app_root / ".generated/python-version.txt").read_text().strip(), "3.14"
        )

    def test_python313_input_is_accepted_but_not_as_python314(self):
        self.wheels("3.13", api=21)
        self.assertEqual(self.prepare("3.13")["python_version"], "3.13")
        with self.assertRaisesRegex(ValueError, "Python 3.14"):
            self.prepare("3.14")

    def test_missing_empty_or_incomplete_wheelhouse_fails(self):
        with self.assertRaisesRegex(ValueError, "does not exist"):
            prepare_app.inspect_wheelhouse(self.root / "missing", "3.14")
        with self.assertRaisesRegex(ValueError, "no directly accessible"):
            self.prepare()
        self.wheels()
        (self.wheelhouse / "msgpack-1.2.2-py3-none-any.whl").unlink()
        with self.assertRaisesRegex(ValueError, "genuinely built msgpack"):
            self.prepare()

    def test_api_above_declared_minimum_fails(self):
        self.wheels(api=26)
        with self.assertRaisesRegex(ValueError, "API <= 24"):
            self.prepare()

    def test_ambiguous_native_inputs_fail(self):
        self.wheels()
        duplicate = self.wheelhouse / "jiter-0.16.0-cp314-cp314-android_21_arm64_v8a.whl"
        duplicate.write_bytes(b"duplicate")
        with self.assertRaisesRegex(ValueError, "found 2"):
            self.prepare()

    def test_generated_source_changes_only_when_prepared_again(self):
        self.wheels()
        before = self.prepare()
        self.smoke.write_text("def main(argv):\n    return 1\n", encoding="utf-8")
        self.assertNotEqual(prepare_app.digest(self.smoke), before["runtime_smoke_sha256"])
        after = self.prepare()
        self.assertEqual(prepare_app.digest(self.smoke), after["runtime_smoke_sha256"])
        self.assertNotEqual(before["runtime_smoke_sha256"], after["runtime_smoke_sha256"])

    def test_unexpected_generated_modules_are_never_packaged(self):
        self.wheels()
        self.prepare()
        sentinel = self.app_root / ".generated/python/unrelated.py"
        sentinel.write_text("USER_WORK = True\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Unexpected generated"):
            self.prepare()
        self.assertEqual(sentinel.read_text(), "USER_WORK = True\n")


if __name__ == "__main__":
    unittest.main()
