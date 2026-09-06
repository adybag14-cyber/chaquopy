import hashlib
from pathlib import Path
import tempfile
import unittest

from compat.hermes.prepare_hermes import complementary_requirements, consumer_requirements, validate_inventory


class HermesConsumerTests(unittest.TestCase):
    def test_sdk_overrides_do_not_drop_complementary_hermes_requirements(self):
        merged = consumer_requirements(
            "PyYAML==6.0.3\nopenai==1.39.0\ntyping_extensions==4.15.0\n",
            "openai==2.54.0\ntyping-extensions==4.16.0\njiter==0.16.0\n",
            "urllib3==2.7.0\n",
        )
        self.assertEqual(merged.splitlines(), [
            "jiter==0.16.0", "openai==2.54.0", "pyyaml==6.0.3",
            "typing-extensions==4.16.0", "urllib3==2.7.0",
        ])
        self.assertEqual(complementary_requirements(merged, "openai==2.54.0\njiter==0.16.0\n"),
                         "pyyaml==6.0.3\ntyping-extensions==4.16.0\nurllib3==2.7.0\n")
        with self.assertRaisesRegex(ValueError, "preserve"):
            complementary_requirements(merged, "openai==2.55.0\n")

    def test_inventory_rejects_changed_extra_and_escaping_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = b"genuine fixture bytes"
            artifact = root / "artifact.whl"
            artifact.write_bytes(payload)
            entry = {"path": "artifact.whl", "bytes": len(payload),
                     "sha256": hashlib.sha256(payload).hexdigest()}
            validate_inventory(root, [entry])
            artifact.write_bytes(payload[:-1] + b"!")
            with self.assertRaisesRegex(ValueError, "Changed"):
                validate_inventory(root, [entry])
            artifact.write_bytes(payload)
            (root / "unplanned.whl").write_bytes(b"unplanned")
            with self.assertRaisesRegex(ValueError, "unrecorded"):
                validate_inventory(root, [entry])
            with self.assertRaisesRegex(ValueError, "Invalid"):
                validate_inventory(root, [{**entry, "path": "../artifact.whl"}])


if __name__ == "__main__":
    unittest.main()
