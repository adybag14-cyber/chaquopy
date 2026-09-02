# Nonproduction Hermes / Chaquopy compatibility app

This separate app is `com.mobilefork.chaquopycompat`, not the production Hermes
package. It embeds genuine distributions with the Chaquopy **17.0.0** plugin and
Java/JNI libraries, plus the fork's **source-built Python bootstrap** (version
from `VERSION.txt`). This contains the split-archive native-loader fix. Android Gradle
Plugin **8.9.3**, Gradle **8.11.1**, minimum Android API **24**, compile/target API
**35**, and both `arm64-v8a` and `x86_64`. Python **3.14** is the default; run 3.13
as the comparison lane. The existing upstream demo uses a different Gradle
toolchain: do not use its wrapper for this project. No new wrapper binary is
committed here. CI can provision Gradle 8.11.1 with `gradle/actions/setup-gradle`.

## Inputs and preparation

The app uses JDK 17 and Android SDK platform 35. Building the fork bootstrap uses
JDK 11 for the upstream Gradle 7.0.2 wrapper and requires SDK platform 36 during
project configuration. CI pins the build interpreters to CPython **3.13.15** and
**3.14.7**; use a matching minor to validate compiled bootstrap code, preferably
the same patch version. The wheelhouse must contain the artifact-audited jiter 0.16.0 and
pydantic-core 2.46.5 Android wheels for **both ABIs** of the selected Python, plus
the genuinely built `msgpack-1.2.2-py3-none-any.whl`. Merge wheels directly into
one directory; pip `--find-links` does not search nested artifact directories.
Run `assemble_wheelhouse.py` first: it downloads and hash-checks official aiohttp
3.14.3 Android wheels and the remaining pinned PyPI wheels into that directory.
The app uses `--no-index`, so Gradle cannot substitute a network package during
assembly. Dependencies are pinned in `requirements.txt`; optional-extension dependencies
have official pure-Python wheels. This is not a source-build fallback or a Hermes
stub substitution.

Build the bootstrap from the repository root with JDK 11 and the selected
`python3.14` on PATH (the task uses `py -3.14` on Windows):

```sh
bash product/gradlew -p product --no-daemon --console=plain runtime:zipPython-3.14
python compat/hermes/prepare_bootstrap.py stage \
  --archive product/runtime/build/bootstrap/bootstrap-3.14.imy \
  --python 3.14 --output /absolute/path/to/fork-bootstrap
```

The helper validates all compiled modules and license/metadata files against the
fork source without executing archived code. It preserves the original archive
bytes and produces a narrowly scoped Maven repository and `receipt.json`.
It refuses a stock, stale or otherwise mismatching bootstrap.

Switch to JDK 17 for the app, still using the selected host Python:

```sh
python compat/hermes/prepare_app.py \
  --wheelhouse /absolute/path/to/wheelhouse --python-version 3.14 \
  --bootstrap /absolute/path/to/fork-bootstrap
gradle -p compat/hermes/android --no-daemon --console=plain \
  -PpythonVersion=3.14 -Pwheelhouse=/absolute/path/to/wheelhouse \
  -Pbootstrap=/absolute/path/to/fork-bootstrap \
  -PbuildPython=/absolute/path/to/python3.14 \
  assembleDebug assembleDebugAndroidTest lintDebug

python compat/hermes/prepare_bootstrap.py verify-apk \
  --apk compat/hermes/android/app/build/outputs/apk/debug/app-debug.apk \
  --receipt /absolute/path/to/fork-bootstrap/receipt.json --python 3.14
```

`prepare_app.py` copies the single canonical `compat/hermes/runtime_smoke.py`
into ignored `.generated/python`, records its SHA-256 and the wheel inventory,
and refuses missing/ambiguous wheel inputs. Gradle rejects a stale smoke copy,
Python-version mismatch, different wheelhouse, or changed bootstrap receipt.
Only the bootstrap module is resolved from the exclusive local Maven repository;
Java/JNI and the plugin remain on stock 17.0.0. Before compilation, Gradle checks
both bootstrap configurations' version, classifier and exact digest. The APK
check then verifies that the same archive bytes are actually embedded.
Generated sources survive
`gradle clean`. The receipt is `.generated/preparation.json`. These preparation
checks record the input bytes; the separate wheel audit verifies archive/ELF
contents. Neither preparation nor APK compilation certifies Android execution.

For Python 3.13, rerun preparation with `--python-version 3.13`, use
`-PpythonVersion=3.13`, and select an actual Python 3.13 build interpreter. Build
and stage its own `runtime:zipPython-3.13` bootstrap first.

## Actual device or emulator gate

Use an explicitly selected device serial. This app's package ID is distinct, so
installing it does not overwrite production Hermes or its data.

```sh
adb -s SERIAL install -r compat/hermes/android/app/build/outputs/apk/debug/app-debug.apk
adb -s SERIAL install -r compat/hermes/android/app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk
adb -s SERIAL shell am instrument -w -r \
  -e class com.mobilefork.chaquopycompat.FullCompatibilityTest \
  com.mobilefork.chaquopycompat.test/androidx.test.runner.AndroidJUnitRunner
adb -s SERIAL shell run-as com.mobilefork.chaquopycompat cat files/runtime-smoke.json
```

Require `OK (1 test)` from instrumentation **and** JSON `status: "passed"`,
`android_execution_verified: true`, a nonzero Python test count, and no skips.
Then run `compat/hermes/verify_app_report.py` on the saved JSON with the exact
`--python`, `--abi`, and observed device `--api`. This verifies the complete
23-test identity set and all pinned genuine packages; a status label or a
nonzero count alone is not sufficient. The hosted matrix does this on API 24
and 35 for both Python versions. Device API is measured independently of the
interpreter's build-time minimum API.
The instrumentation calls `runtime_smoke.main(["--package", "full",
"--require-android", "--json-output", ...])` and asserts its zero exit code.
The Python suite exercises real native imports, Pydantic behavior, pure msgpack,
aiohttp HTTP/WebSocket loopback fixtures, and genuine OpenAI/Anthropic/FAL SDKs
with in-memory transports. No paid endpoint, service credential, or real model
inference is involved. An `x86_64` emulator pass is not an ARM64 device pass:
retain separate Python-version and ABI evidence. Re-run in a fresh instrumented
process for each lane rather than relying on a previously imported runtime.

The launcher intentionally displays only test instructions. Opening it is not
compatibility proof. This app has no release signing or publication workflow.
