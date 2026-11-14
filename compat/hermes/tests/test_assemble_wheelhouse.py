"""Offline wheelhouse tests with synthetic wheels and mocked PyPI bytes."""

import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.request import Request
import zipfile

from compat.hermes import assemble_wheelhouse as assembler
from compat.hermes import audit_wheels as auditor
from compat.hermes import runtime_smoke, verify_app_report
from compat.hermes.tests.test_audit_wheels import elf, wheel_files, with_record


def encode(value):
    return json.dumps(value, sort_keys=True).encode()


def native_runtime_report(package, python):
    """Synthetic complete report, never represented as real device evidence."""
    soabi = f"cpython-{python.replace('.', '')}-x86_64-linux-android"
    version, module, native_names = runtime_smoke.PACKAGE_SPECS[package]
    passed = sorted(verify_app_report.expected_cases(package))
    return {
        "schema_version": 1,
        "status": "passed",
        "package_group": package,
        "android_execution_verified": True,
        "require_android": True,
        "platform": {
            "platform": "android",
            "is_android": True,
            "python": python + ".7",
            "implementation": "CPython",
            "soabi": soabi,
            "machine": "x86_64",
            "android_api_level": 27,
            "page_size": 4096,
        },
        "tests": {
            "run": len(passed),
            "passed": ["runtime_smoke." + case for case in passed],
            "failures": [],
            "errors": [],
            "skipped": [],
        },
        "packages": [
            {
                "distribution": package,
                "version": version,
                "expected_version": version,
                "module": {
                    "module": module,
                    "native": False,
                    "file": f"/data/fixture/{module.replace('.', '/')}/__init__.py",
                },
                "native_modules": [
                    {
                        "module": name,
                        "native": True,
                        "file": "/data/fixture/" + name.replace(".", "/") + f".{soabi}.so",
                    }
                    for name in native_names
                ],
            }
        ],
        "network_scope": "loopback-and-in-memory-fixtures-only",
        "service_inference_verified": False,
    }


def wheel_bytes(
    name,
    version,
    tag,
    *,
    abi=None,
    python="3.14",
    requires=(),
    requires_python=None,
    alignment=16384,
):
    if abi:
        extension = {"pydantic_core": "_pydantic_core", "aiohttp": "_http_parser"}.get(name, name)
        cpu = "aarch64" if abi == "arm64-v8a" else "x86_64"
        filename = f"{name}/{extension}.cpython-{python.replace('.', '')}-{cpu}-linux-android.so"
        data = {
            filename: elf(
                machine=183 if abi == "arm64-v8a" else 62,
                align=alignment,
                needed=(f"libpython{python}.so", "libc.so"),
                initializer=f"PyInit_{extension}",
            )
        }
    else:
        data = {f"{name}/__init__.py": b"# genuine pure-Python test fixture\n"}
    files, dist, filename = wheel_files(
        name=name, version=version, tag=tag, data=data, pure=abi is None
    )
    metadata = f"{dist}/METADATA"
    for requirement in requires:
        files[metadata] += f"Requires-Dist: {requirement}\n".encode()
    if requires_python:
        files[metadata] += f"Requires-Python: {requires_python}\n".encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, data in with_record(files, dist).items():
            archive.writestr(path, data)
    return filename, output.getvalue()


class AssemblyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.downloads = self.root / "downloads"
        self.downloads.mkdir()
        self.output = self.root / "complete-wheels"
        self.requirements = self.root / "requirements.txt"
        self.requirements.write_text(
            "jiter==0.19.0\npydantic-core==2.50.4\nmsgpack==1.2.4\n"
            "pydantic==2.14.3\naiohttp==3.14.3\nhelper==1.0\n"
        )
        self.lock_path = self.root / "sources.lock.json"
        self.lock_bytes = assembler.DEFAULT_LOCK.read_bytes()
        self.lock = json.loads(self.lock_bytes)
        self.lock_path.write_bytes(self.lock_bytes)
        self.network = {}
        self.fetches = []
        self.make_artifacts()
        self.add_official("pydantic", "2.14.3", requires=("pydantic-core==2.50.4",))
        self.add_official("helper", "1.0")
        self.add_official("aiohttp", "3.14.3", native=True)

    def make_artifacts(self, python="3.14"):
        for package in ("jiter", "pydantic-core", "msgpack"):
            key = package.replace("-", "_")
            entry = self.lock["packages"][key]
            for abi in assembler.ABIS if package != "msgpack" else (None,):
                name = (
                    f"hermes-native-{package}-{python}-{abi}"
                    if abi
                    else "hermes-msgpack-pure-python"
                )
                artifact = self.downloads / name
                artifact.mkdir(exist_ok=True)
                wheels = artifact / "wheels"
                wheels.mkdir(exist_ok=True)
                tag = (
                    f"cp{python.replace('.', '')}-cp{python.replace('.', '')}"
                    f"-android_24_{abi.replace('-', '_')}"
                    if abi
                    else "py3-none-any"
                )
                filename, data = wheel_bytes(key, entry["version"], tag, abi=abi, python=python)
                (wheels / filename).write_bytes(data)
                audit = auditor.audit_directory(wheels, python if abi else "3.14", abi or "x86_64")
                self.assertTrue(audit["passed"], audit)
                (artifact / "wheel-audit.json").write_bytes(encode(audit))
                provenance = {
                    "schema_version": 1,
                    "package": key,
                    "version": entry["version"],
                    "source_lock_sha256": hashlib.sha256(self.lock_bytes).hexdigest(),
                    "source_archive": {
                        field: entry[field] for field in ("url", "filename", "sha256", "bytes")
                    },
                    "metadata_url": entry["metadata_url"],
                    "targets": self.lock["targets"],
                    "build_environment": entry["build"].get("environment", {}),
                    "patches": [],
                    "android_compatibility_verified": False,
                }
                if abi:
                    provenance["patches"] = [
                        {
                            "path": "pyproject.toml",
                            "kind": "pin-build-requirement",
                            "from": entry["build"]["original_requirement"],
                            "to": entry["build"]["pinned_requirement"],
                            "before_sha256": "a" * 64,
                            "after_sha256": "b" * 64,
                        }
                    ]
                    report = {
                        "schema": "hermes-android-native-build-v1",
                        "build_status": "passed",
                        "package": package,
                        "python": python,
                        "abi": abi,
                        "android_api": 24,
                        "source_lock_sha256": provenance["source_lock_sha256"],
                        "cibuildwheel_version": "4.3.5",
                        "rustc_version": "rustc 1.88.0 (fixture)",
                        "runtime_validation": "not_run_cross_architecture"
                        if abi == "arm64-v8a"
                        else "cibuildwheel-android-testbed-passed",
                    }
                    if abi == "x86_64":
                        runtime = native_runtime_report(package, python)
                        payload = encode(runtime)
                        (artifact / "runtime-report.json").write_bytes(payload)
                        report.update(
                            runtime_report_sha256=hashlib.sha256(payload).hexdigest(),
                            runtime_android_api=runtime["platform"]["android_api_level"],
                            runtime_page_size=runtime["platform"]["page_size"],
                            runtime_python=runtime["platform"]["python"],
                        )
                    (artifact / "build-report.json").write_bytes(encode(report))
                if "paired_requirement" in entry:
                    provenance["paired_requirement"] = entry["paired_requirement"]
                (artifact / "source-provenance.json").write_bytes(encode(provenance))

    def add_official(
        self,
        name,
        version,
        *,
        native=False,
        python="3.14",
        requires=(),
        requires_python=None,
        alignment=16384,
    ):
        entries = []
        for abi in assembler.ABIS if native else (None,):
            tag = (
                f"cp{python.replace('.', '')}-cp{python.replace('.', '')}"
                f"-android_24_{abi.replace('-', '_')}"
                if abi
                else "py3-none-any"
            )
            filename, data = wheel_bytes(
                name,
                version,
                tag,
                abi=abi,
                python=python,
                requires=requires,
                requires_python=requires_python,
                alignment=alignment,
            )
            url = f"https://files.pythonhosted.org/packages/aa/bb/{filename}"
            entries.append(
                {
                    "filename": filename,
                    "url": url,
                    "size": len(data),
                    "digests": {"sha256": hashlib.sha256(data).hexdigest()},
                    "packagetype": "bdist_wheel",
                    "yanked": False,
                    "requires_python": requires_python,
                }
            )
            self.network[url] = data
        metadata_url = f"https://pypi.org/pypi/{name}/{version}/json"
        self.network[metadata_url] = encode(
            {"info": {"name": name, "version": version}, "urls": entries}
        )

    def fetch(self, url, limit):
        self.fetches.append(url)
        self.assertIn(url, self.network, "test unexpectedly tried an unmocked network URL")
        result = self.network[url]
        if len(result) > limit:
            raise assembler.AssemblyError("download exceeds declared byte limit")
        return result

    def assemble(self, python="3.14"):
        return assembler.assemble(
            self.downloads,
            python,
            self.output,
            requirements_path=self.requirements,
            lock_path=self.lock_path,
            fetch=self.fetch,
        )

    def mutate(self, relative, callback):
        path = self.downloads / relative
        value = json.loads(path.read_bytes())
        callback(value)
        path.write_bytes(encode(value))

    def mutate_runtime(self, callback, *, rebind=True, package="jiter"):
        artifact = f"hermes-native-{package}-3.14-x86_64"
        relative = f"{artifact}/runtime-report.json"
        self.mutate(relative, callback)
        if rebind:
            payload = (self.downloads / relative).read_bytes()
            self.mutate(
                f"{artifact}/build-report.json",
                lambda build: build.update(
                    runtime_report_sha256=hashlib.sha256(payload).hexdigest()
                ),
            )

    def metadata(self, package="helper", version="1.0", callback=None):
        url = f"https://pypi.org/pypi/{package}/{version}/json"
        value = json.loads(self.network[url])
        if callback:
            callback(value)
            self.network[url] = encode(value)
        return value

    def test_complete_atomic_wheelhouse(self):
        report = self.assemble()
        self.assertTrue(report["passed"])
        self.assertFalse(report["runtime_tested"])
        self.assertEqual(len(report["wheels"]), 9)
        self.assertTrue(all(item["passed"] for item in report["static_audits"].values()))
        self.assertEqual(len(list(self.output.glob("*.whl"))), 9)
        self.assertFalse(list(self.root.glob(".hermes-wheelhouse-*")))
        self.assertEqual(
            json.loads((self.output / "wheelhouse-manifest.json").read_bytes()), report
        )
        arm_evidence = next(
            wheel
            for wheel in report["wheels"]
            if wheel["package"] == "jiter" and wheel["abis"] == ["arm64-v8a"]
        )
        self.assertEqual(
            arm_evidence["source"]["evidence"]["reported_runtime_validation"],
            "not_run_cross_architecture",
        )
        self.assertFalse(arm_evidence["source"]["evidence"]["runtime_evidence_verified"])
        x86 = next(
            wheel
            for wheel in report["wheels"]
            if wheel["package"] == "jiter" and wheel["abis"] == ["x86_64"]
        )["source"]["evidence"]
        self.assertTrue(x86["runtime_evidence_verified"])
        self.assertEqual(x86["runtime_android_api"], 27)
        self.assertEqual(x86["runtime_page_size"], 4096)
        self.assertEqual(x86["runtime_python"], "3.14.7")
        self.assertEqual(x86["runtime_package_group"], "jiter")
        self.assertEqual(x86["runtime_tests_passed"], 3)
        self.assertEqual(report["android_api"], 24)
        self.assertEqual(report["app_python_full_version"], "3.14.0")

    def test_python313_and_shared_msgpack(self):
        self.make_artifacts("3.13")
        self.add_official("aiohttp", "3.14.3", native=True, python="3.13")
        self.assertEqual(self.assemble("3.13")["app_python_full_version"], "3.13.9")

    def test_existing_output_is_preserved(self):
        self.output.mkdir()
        marker = self.output / "user.txt"
        marker.write_text("preserve")
        with self.assertRaisesRegex(assembler.AssemblyError, "new directory"):
            self.assemble()
        self.assertEqual(marker.read_text(), "preserve")

    def test_missing_artifact_fails_before_network(self):
        path = self.downloads / "hermes-native-jiter-3.14-arm64-v8a"
        path.rename(path.with_name("renamed"))
        with self.assertRaisesRegex(assembler.AssemblyError, "missing artifact"):
            self.assemble()
        self.assertFalse(self.fetches)
        self.assertFalse(self.output.exists())

    def test_failed_build_is_not_accepted(self):
        self.mutate(
            "hermes-native-jiter-3.14-arm64-v8a/build-report.json",
            lambda report: report.update(build_status="failed"),
        )
        with self.assertRaisesRegex(assembler.AssemblyError, "build did not pass"):
            self.assemble()

    def test_old_label_only_runtime_evidence_is_rejected(self):
        def strip_binding(build):
            for key in tuple(build):
                if key.startswith("runtime_") and key != "runtime_validation":
                    build.pop(key)

        self.mutate("hermes-native-jiter-3.14-x86_64/build-report.json", strip_binding)
        with self.assertRaisesRegex(assembler.AssemblyError, "complete runtime report binding"):
            self.assemble()
        self.assertFalse(self.fetches)
        self.assertFalse(self.output.exists())

    def test_missing_runtime_report(self):
        path = self.downloads / "hermes-native-jiter-3.14-x86_64/runtime-report.json"
        path.rename(path.with_suffix(".missing"))
        with self.assertRaisesRegex(assembler.AssemblyError, "missing or linked JSON receipt"):
            self.assemble()

    def test_runtime_report_hash_mismatch(self):
        self.mutate_runtime(lambda report: report.update(elapsed_seconds=900), rebind=False)
        with self.assertRaisesRegex(assembler.AssemblyError, "runtime report SHA-256 mismatch"):
            self.assemble()

    def test_invalid_runtime_report_hash(self):
        self.mutate(
            "hermes-native-jiter-3.14-x86_64/build-report.json",
            lambda report: report.update(runtime_report_sha256="bad"),
        )
        with self.assertRaisesRegex(assembler.AssemblyError, "SHA-256 binding"):
            self.assemble()

    def test_x86_may_not_claim_cross_architecture_skip(self):
        self.mutate(
            "hermes-native-jiter-3.14-x86_64/build-report.json",
            lambda report: report.update(runtime_validation="not_run_cross_architecture"),
        )
        with self.assertRaisesRegex(assembler.AssemblyError, "requires passing Android runtime"):
            self.assemble()

    def test_arm_may_not_claim_runtime_pass(self):
        self.mutate(
            "hermes-native-jiter-3.14-arm64-v8a/build-report.json",
            lambda report: report.update(runtime_validation="cibuildwheel-android-testbed-passed"),
        )
        with self.assertRaisesRegex(assembler.AssemblyError, "ARM.*must not claim"):
            self.assemble()

    def test_arm_may_not_attach_runtime_report(self):
        path = self.downloads / "hermes-native-jiter-3.14-arm64-v8a/runtime-report.json"
        path.write_bytes(encode(native_runtime_report("jiter", "3.14")))
        with self.assertRaisesRegex(assembler.AssemblyError, "ARM.*unexpected runtime evidence"):
            self.assemble()

    def test_arm_may_not_attach_runtime_metadata(self):
        self.mutate(
            "hermes-native-jiter-3.14-arm64-v8a/build-report.json",
            lambda report: report.update(runtime_android_api=27),
        )
        with self.assertRaisesRegex(assembler.AssemblyError, "ARM.*unexpected runtime evidence"):
            self.assemble()

    def test_wrong_runtime_group_rejected_even_with_rebound_hash(self):
        self.mutate_runtime(lambda report: report.update(package_group="pydantic-core"))
        with self.assertRaisesRegex(assembler.AssemblyError, "selected runtime suite did not pass"):
            self.assemble()

    def test_runtime_host_platform_rejected(self):
        self.mutate_runtime(lambda report: report["platform"].update(platform="linux"))
        with self.assertRaisesRegex(assembler.AssemblyError, "not actual Android"):
            self.assemble()

    def test_runtime_wrong_machine_rejected(self):
        self.mutate_runtime(lambda report: report["platform"].update(machine="aarch64"))
        with self.assertRaisesRegex(assembler.AssemblyError, "selected ABI mismatch"):
            self.assemble()

    def test_runtime_wrong_python_rejected(self):
        self.mutate_runtime(lambda report: report["platform"].update(python="3.13.9"))
        with self.assertRaisesRegex(assembler.AssemblyError, "selected Python mismatch"):
            self.assemble()

    def test_runtime_wrong_soabi_rejected(self):
        self.mutate_runtime(
            lambda report: report["platform"].update(soabi="cpython-314-aarch64-linux-android")
        )
        with self.assertRaisesRegex(assembler.AssemblyError, "SOABI mismatch"):
            self.assemble()

    def test_incomplete_runtime_suite_rejected(self):
        self.mutate_runtime(
            lambda report: report["tests"].update(run=1, passed=report["tests"]["passed"][:1])
        )
        with self.assertRaisesRegex(assembler.AssemblyError, "required test count mismatch"):
            self.assemble()

    def test_skipped_runtime_case_rejected(self):
        self.mutate_runtime(lambda report: report["tests"].update(skipped=["unavailable"]))
        with self.assertRaisesRegex(assembler.AssemblyError, "failed or skipped tests"):
            self.assemble()

    def test_duplicate_runtime_cases_rejected(self):
        self.mutate_runtime(
            lambda report: report["tests"].update(passed=[report["tests"]["passed"][0]] * 3)
        )
        with self.assertRaisesRegex(assembler.AssemblyError, "duplicate.*passed-test"):
            self.assemble()

    def test_wrong_runtime_package_version_rejected(self):
        self.mutate_runtime(lambda report: report["packages"][0].update(version="0.1.0"))
        with self.assertRaisesRegex(assembler.AssemblyError, "pinned version mismatch"):
            self.assemble()

    def test_missing_native_runtime_module_rejected(self):
        self.mutate_runtime(lambda report: report["packages"][0].update(native_modules=[]))
        with self.assertRaisesRegex(assembler.AssemblyError, "native module evidence count"):
            self.assemble()

    def test_runtime_did_not_require_android_rejected(self):
        self.mutate_runtime(lambda report: report.update(require_android=False))
        with self.assertRaisesRegex(assembler.AssemblyError, "Android-required execution"):
            self.assemble()

    def test_runtime_api_is_not_inferred_from_static_api24(self):
        self.mutate(
            "hermes-native-jiter-3.14-x86_64/build-report.json",
            lambda report: report.update(runtime_android_api=24),
        )
        with self.assertRaisesRegex(assembler.AssemblyError, "exact Android API mismatch"):
            self.assemble()

    def test_runtime_page_size_must_equal_build_report(self):
        self.mutate(
            "hermes-native-jiter-3.14-x86_64/build-report.json",
            lambda report: report.update(runtime_page_size=16384),
        )
        with self.assertRaisesRegex(
            assembler.AssemblyError, "runtime report/build metadata mismatch"
        ):
            self.assemble()

    def test_runtime_python_micro_must_equal_build_report(self):
        self.mutate(
            "hermes-native-jiter-3.14-x86_64/build-report.json",
            lambda report: report.update(runtime_python="3.14.0"),
        )
        with self.assertRaisesRegex(
            assembler.AssemblyError, "runtime report/build metadata mismatch"
        ):
            self.assemble()

    def test_runtime_metadata_must_use_exact_integer_types(self):
        self.mutate(
            "hermes-native-jiter-3.14-x86_64/build-report.json",
            lambda report: report.update(runtime_android_api=27.0),
        )
        with self.assertRaisesRegex(assembler.AssemblyError, "invalid measured runtime"):
            self.assemble()

    def test_receipt_validation_does_not_execute_tests(self):
        with (
            patch.object(runtime_smoke, "run", side_effect=AssertionError("must not run tests")),
            patch.object(
                runtime_smoke, "package_evidence", side_effect=AssertionError("no imports")
            ),
        ):
            self.assertTrue(self.assemble()["passed"])

    def test_source_archive_hash_mismatch(self):
        self.mutate(
            "hermes-native-jiter-3.14-arm64-v8a/source-provenance.json",
            lambda report: report["source_archive"].update(sha256="0" * 64),
        )
        with self.assertRaisesRegex(assembler.AssemblyError, "archive provenance"):
            self.assemble()

    def test_source_lock_hash_mismatch(self):
        self.mutate(
            "hermes-native-jiter-3.14-arm64-v8a/source-provenance.json",
            lambda report: report.update(source_lock_sha256="0" * 64),
        )
        with self.assertRaisesRegex(assembler.AssemblyError, "lock mismatch"):
            self.assemble()

    def test_source_patch_mismatch(self):
        self.mutate(
            "hermes-native-jiter-3.14-arm64-v8a/source-provenance.json",
            lambda report: report["patches"][0].update(to="maturin==0.1"),
        )
        with self.assertRaisesRegex(assembler.AssemblyError, "unexpected source patch"):
            self.assemble()

    def test_msgpack_requires_pure_build_environment(self):
        self.mutate(
            "hermes-msgpack-pure-python/source-provenance.json",
            lambda report: report.update(build_environment={}),
        )
        with self.assertRaisesRegex(assembler.AssemblyError, "environment mismatch"):
            self.assemble()

    def test_wrong_native_target(self):
        self.mutate(
            "hermes-native-jiter-3.14-arm64-v8a/build-report.json",
            lambda report: report.update(python="3.13"),
        )
        with self.assertRaisesRegex(assembler.AssemblyError, "target mismatch"):
            self.assemble()

    def test_failed_audit(self):
        self.mutate(
            "hermes-native-jiter-3.14-arm64-v8a/wheel-audit.json",
            lambda report: report.update(passed=False),
        )
        with self.assertRaisesRegex(assembler.AssemblyError, "audit did not pass"):
            self.assemble()

    def test_custom_wheel_hash_tamper(self):
        path = next((self.downloads / "hermes-native-jiter-3.14-arm64-v8a/wheels").glob("*.whl"))
        data = path.read_bytes()
        path.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
        with self.assertRaisesRegex(assembler.AssemblyError, "SHA-256 mismatch"):
            self.assemble()

    def test_extra_custom_wheel_is_rejected(self):
        path = self.downloads / "hermes-native-jiter-3.14-arm64-v8a/wheels/extra.whl"
        path.write_bytes(b"extra")
        with self.assertRaisesRegex(assembler.AssemblyError, "does not match its audit"):
            self.assemble()

    def test_no_sdist_fallback(self):
        self.metadata(callback=lambda metadata: metadata["urls"][0].update(packagetype="sdist"))
        with self.assertRaisesRegex(assembler.AssemblyError, "pure-Python wheel"):
            self.assemble()
        self.assertFalse(self.output.exists())

    def test_yanked_wheel_rejected(self):
        self.metadata(callback=lambda metadata: metadata["urls"][0].update(yanked=True))
        with self.assertRaisesRegex(assembler.AssemblyError, "pure-Python wheel"):
            self.assemble()

    def test_official_wrong_metadata_identity(self):
        self.metadata(callback=lambda metadata: metadata["info"].update(version="2.0"))
        with self.assertRaisesRegex(assembler.AssemblyError, "identity mismatch"):
            self.assemble()

    def test_official_bad_hash(self):
        self.metadata(
            callback=lambda metadata: metadata["urls"][0]["digests"].update(sha256="0" * 64)
        )
        with self.assertRaisesRegex(assembler.AssemblyError, "SHA-256 mismatch"):
            self.assemble()

    def test_official_bad_length(self):
        self.metadata(callback=lambda metadata: metadata["urls"][0].update(size=1))
        with self.assertRaisesRegex(assembler.AssemblyError, "byte limit"):
            self.assemble()

    def test_evil_official_url(self):
        self.metadata(
            callback=lambda metadata: metadata["urls"][0].update(
                url="https://example.com/helper.whl"
            )
        )
        with self.assertRaisesRegex(assembler.AssemblyError, "official PyPI"):
            self.assemble()

    def test_aiohttp_missing_second_abi(self):
        self.metadata("aiohttp", "3.14.3", callback=lambda metadata: metadata["urls"].pop())
        with self.assertRaisesRegex(assembler.AssemblyError, "no official"):
            self.assemble()

    def test_official_4kb_elf_fails_fresh_audit(self):
        self.add_official("aiohttp", "3.14.3", native=True, alignment=4096)
        with self.assertRaisesRegex(assembler.AssemblyError, "static audit failed"):
            self.assemble()

    def test_offline_dependency_missing(self):
        self.add_official("helper", "1.0", requires=("missing>=1",))
        with self.assertRaisesRegex(assembler.AssemblyError, "closure missing"):
            self.assemble()

    def test_dependency_version_conflict(self):
        self.add_official("helper", "1.0", requires=("pydantic<2",))
        with self.assertRaisesRegex(assembler.AssemblyError, "dependency conflict"):
            self.assemble()

    def test_android_markers_not_build_host(self):
        self.add_official("helper", "1.0", requires=('missing; sys_platform == "win32"',))
        self.assertTrue(self.assemble()["passed"])

    def test_android_active_marker(self):
        self.add_official("helper", "1.0", requires=('missing; sys_platform == "android"',))
        with self.assertRaisesRegex(assembler.AssemblyError, "closure missing"):
            self.assemble()

    def test_dependency_extras_propagate(self):
        self.add_official("helper", "1.0", requires=("pydantic[special]==2.14.3",))
        self.add_official("pydantic", "2.14.3", requires=('missing; extra == "special"',))
        with self.assertRaisesRegex(assembler.AssemblyError, "closure missing"):
            self.assemble()

    def test_requires_python_uses_app_micro_not_host(self):
        self.add_official("helper", "1.0", requires_python=">=3.14.1")
        with self.assertRaisesRegex(assembler.AssemblyError, "pure-Python wheel"):
            self.assemble()

    def test_duplicate_requirement(self):
        self.requirements.write_text(self.requirements.read_text() + "pydantic_core==2.50.4\n")
        with self.assertRaisesRegex(assembler.AssemblyError, "duplicate pinned"):
            self.assemble()

    def test_unpinned_requirement(self):
        self.requirements.write_text(self.requirements.read_text() + "unlocked>=1\n")
        with self.assertRaisesRegex(assembler.AssemblyError, "one exact version"):
            self.assemble()

    def test_unexpected_core_version(self):
        self.requirements.write_text(self.requirements.read_text().replace("2.50.4", "2.48.0"))
        with self.assertRaisesRegex(assembler.AssemblyError, "version mismatch"):
            self.assemble()

    def test_output_inside_input_rejected(self):
        self.output = self.downloads / "output"
        with self.assertRaisesRegex(assembler.AssemblyError, "inside downloads"):
            self.assemble()

    def test_invalid_urls(self):
        invalid = (
            "http://files.pythonhosted.org/packages/one.whl",
            "https://files.pythonhosted.org.evil.test/packages/one.whl",
            "https://files.pythonhosted.org:443/packages/one.whl",
            "https://user@files.pythonhosted.org/packages/one.whl",
            "https://files.pythonhosted.org/packages/../one.whl",
            "https://files.pythonhosted.org/packages/%2e%2e/one.whl",
            "https://files.pythonhosted.org/packages/one.whl?token=abc",
        )
        for url in invalid:
            with (
                self.subTest(url=url),
                self.assertRaises((assembler.AssemblyError, auditor.AuditError)),
            ):
                assembler.validate_url(url)

    def test_redirect_to_untrusted_host_rejected_before_request(self):
        redirect = assembler.OfficialRedirects()
        request = Request("https://files.pythonhosted.org/packages/one.whl")
        with self.assertRaisesRegex(assembler.AssemblyError, "official PyPI"):
            redirect.redirect_request(request, None, 302, "found", {}, "https://evil.test/one.whl")

    def test_ambiguous_pure_wheel_fails(self):
        self.metadata(
            callback=lambda metadata: metadata["urls"].append(copy.deepcopy(metadata["urls"][0]))
        )
        with self.assertRaisesRegex(assembler.AssemblyError, "found 2"):
            self.assemble()

    def test_cli_failure_does_not_create_output(self):
        with patch.object(
            assembler, "assemble", side_effect=assembler.AssemblyError("fixture failure")
        ):
            code = assembler.main(
                [
                    "--downloads",
                    str(self.downloads),
                    "--python",
                    "3.14",
                    "--output",
                    str(self.output),
                ]
            )
        self.assertEqual(code, 1)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
