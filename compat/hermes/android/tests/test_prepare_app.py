"""Offline app staging tests with explicitly mocked bootstrap validation.

The dummy wheels/receipts are unit fixtures, not content or Android-runtime proof.
The bootstrap helper's source, archive, and bytecode checks have their own tests.
"""

import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from compat.hermes import prepare_app, prepare_bootstrap


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
        self.bootstrap = self.root / "bootstrap"
        (self.bootstrap / "maven").mkdir(parents=True)
        self.bootstrap_data = {
            "schema": "unit-fixture-with-mocked-validation",
            "python": "3.14",
            "version": "17.0.1",
            "archive_path": "maven/bootstrap-17.0.1-3.14.imy",
            "archive_sha256": "b" * 64,
            "archive_bytes": 100,
            "importer_source_sha256": "c" * 64,
            "importer_code_sha256": "d" * 64,
            "source_lock_sha256": "e" * 64,
            "input_sha256": "f" * 64,
            "source_inventory_sha256": "a" * 64,
            "pyc_magic_hex": "deadbeef",
        }
        self.write_bootstrap()
        validator = patch.object(
            prepare_bootstrap,
            "validate_receipt",
            side_effect=self.mocked_validation,
        )
        self.validator = validator.start()
        self.addCleanup(validator.stop)

    def write_bootstrap(self):
        (self.bootstrap / "receipt.json").write_text(
            json.dumps(self.bootstrap_data, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def mocked_validation(self, path, *, expected_python):
        self.assertEqual(path, self.bootstrap / "receipt.json")
        receipt = json.loads(path.read_bytes())
        if receipt["python"] != expected_python:
            raise ValueError("Receipt Python mismatch (mocked helper)")
        return receipt | {
            "receipt_path": str(path.resolve()),
            "maven_repo_path": str(self.bootstrap / "maven"),
        }

    def wheels(self, version="3.14", api=24):
        tag = "cp" + version.replace(".", "")
        for package, package_version in prepare_app.NATIVE_PACKAGES.items():
            for abi in prepare_app.ABIS:
                name = f"{package}-{package_version}-{tag}-{tag}-android_{api}_{abi}.whl"
                (self.wheelhouse / name).write_bytes(b"input inventory fixture, not a real wheel")
        (self.wheelhouse / "msgpack-1.2.2-py3-none-any.whl").write_bytes(b"pure wheel fixture")

    def prepare(self, version="3.14"):
        return prepare_app.prepare_app(
            self.wheelhouse,
            version,
            android_root=self.app_root,
            runtime_source=self.smoke,
            bootstrap=self.bootstrap,
        )

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
        provenance = receipt["bootstrap_provenance"]
        self.assertEqual(provenance["directory"], str(self.bootstrap.resolve()))
        self.assertEqual(provenance["version"], "17.0.1")
        self.assertEqual(provenance["archive_sha256"], self.bootstrap_data["archive_sha256"])
        self.assertEqual(provenance["importer_source_sha256"], "c" * 64)
        receipt_hash = hashlib.sha256((self.bootstrap / "receipt.json").read_bytes()).hexdigest()
        expected_stamps = {
            "bootstrap-path.txt": str(self.bootstrap.resolve()),
            "bootstrap-version.txt": "17.0.1",
            "bootstrap-receipt.sha256": receipt_hash,
            "bootstrap-archive.sha256": "b" * 64,
        }
        for name, content in expected_stamps.items():
            self.assertEqual((self.app_root / ".generated" / name).read_text(), content + "\n")
        self.assertEqual(provenance["receipt_sha256"], receipt_hash)
        self.validator.assert_called_once_with(
            self.bootstrap / "receipt.json",
            expected_python="3.14",
        )

    def test_python313_input_is_accepted_but_not_as_python314(self):
        self.wheels("3.13", api=21)
        self.bootstrap_data["python"] = "3.13"
        self.write_bootstrap()
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

    def test_bootstrap_is_required_keyword(self):
        self.wheels()
        with self.assertRaisesRegex(TypeError, "bootstrap"):
            prepare_app.prepare_app(self.wheelhouse, android_root=self.app_root)
        self.validator.assert_not_called()

    def test_missing_bootstrap_directory_fails_before_generation(self):
        self.wheels()
        with self.assertRaisesRegex(ValueError, "existing real directory"):
            prepare_app.prepare_app(
                self.wheelhouse,
                bootstrap=self.root / "missing-bootstrap",
                android_root=self.app_root,
                runtime_source=self.smoke,
            )
        self.validator.assert_not_called()
        self.assertFalse((self.app_root / ".generated").exists())

    def test_missing_bootstrap_receipt_fails(self):
        self.wheels()
        (self.bootstrap / "receipt.json").unlink()
        with self.assertRaisesRegex(ValueError, "Missing or linked.*receipt"):
            self.prepare()
        self.validator.assert_not_called()
        self.assertFalse((self.app_root / ".generated").exists())

    def test_helper_rejection_preserves_existing_generated_state(self):
        self.wheels()
        self.prepare()
        generated = self.app_root / ".generated"
        before = {
            path.relative_to(generated): path.read_bytes()
            for path in generated.rglob("*")
            if path.is_file()
        }
        self.smoke.write_text("CHANGED = True\n", encoding="utf-8")
        self.validator.side_effect = ValueError("PYC does not match current fork source")
        with self.assertRaisesRegex(ValueError, "current fork source"):
            self.prepare()
        after = {
            path.relative_to(generated): path.read_bytes()
            for path in generated.rglob("*")
            if path.is_file()
        }
        self.assertEqual(before, after)

    def test_wrong_bootstrap_python_is_rejected_by_helper(self):
        self.wheels()
        self.bootstrap_data["python"] = "3.13"
        self.write_bootstrap()
        with self.assertRaisesRegex(ValueError, "Receipt Python mismatch"):
            self.prepare()
        self.validator.assert_called_once_with(
            self.bootstrap / "receipt.json",
            expected_python="3.14",
        )
        self.assertFalse((self.app_root / ".generated").exists())

    def test_wrong_python_returned_by_helper_fails_closed(self):
        self.wheels()
        verified = self.mocked_validation(self.bootstrap / "receipt.json", expected_python="3.14")
        verified["python"] = "3.13"
        self.validator.side_effect = None
        self.validator.return_value = verified
        with self.assertRaisesRegex(ValueError, "Verified bootstrap Python"):
            self.prepare()

    def test_wrong_receipt_or_maven_path_returned_by_helper_is_rejected(self):
        self.wheels()
        verified = self.mocked_validation(self.bootstrap / "receipt.json", expected_python="3.14")
        self.validator.side_effect = None
        for field in ("receipt_path", "maven_repo_path"):
            with self.subTest(field=field):
                self.validator.return_value = verified | {field: str(self.root / "elsewhere")}
                with self.assertRaisesRegex(ValueError, "paths differ"):
                    self.prepare()

    def test_receipt_changed_while_helper_validates_is_rejected(self):
        self.wheels()

        def changing_receipt(path, *, expected_python):
            result = self.mocked_validation(path, expected_python=expected_python)
            path.write_bytes(path.read_bytes() + b" ")
            return result

        self.validator.side_effect = changing_receipt
        with self.assertRaisesRegex(ValueError, "changed during validation"):
            self.prepare()

    def test_invalid_helper_output_is_rejected(self):
        self.wheels()
        self.validator.side_effect = None
        for value in (None, {}, {"version": "17.0.1"}):
            with self.subTest(value=value):
                self.validator.return_value = value
                with self.assertRaisesRegex(ValueError, "verified receipt|incomplete provenance"):
                    self.prepare()

    def test_modified_helper_provenance_is_not_stamped(self):
        self.wheels()
        verified = self.mocked_validation(self.bootstrap / "receipt.json", expected_python="3.14")
        verified["archive_sha256"] = "0" * 64
        self.validator.side_effect = None
        self.validator.return_value = verified
        with self.assertRaisesRegex(ValueError, "differs from receipt bytes"):
            self.prepare()

    def test_cli_requires_bootstrap(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            prepare_app.main(["--wheelhouse", str(self.wheelhouse)])
        self.assertEqual(error.exception.code, 2)

    def test_cli_passes_selected_bootstrap_and_python(self):
        with patch.object(prepare_app, "prepare_app", return_value={}) as stage:
            with contextlib.redirect_stdout(io.StringIO()):
                code = prepare_app.main(
                    [
                        "--wheelhouse",
                        str(self.wheelhouse),
                        "--bootstrap",
                        str(self.bootstrap),
                        "--python-version",
                        "3.13",
                    ]
                )
        self.assertEqual(code, 0)
        stage.assert_called_once_with(self.wheelhouse, "3.13", bootstrap=self.bootstrap)

    def test_cli_unavailable_helper_fails_closed(self):
        with patch.object(prepare_app, "prepare_app", side_effect=ImportError("missing helper")):
            with contextlib.redirect_stderr(io.StringIO()):
                code = prepare_app.main(
                    [
                        "--wheelhouse",
                        str(self.wheelhouse),
                        "--bootstrap",
                        str(self.bootstrap),
                    ]
                )
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
