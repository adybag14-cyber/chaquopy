# Hermes stable Android Python compatibility

This fork-specific laboratory qualifies genuine Python dependencies for Hermes on
CPython **3.13 and 3.14**, Android **API 24+**, and **arm64-v8a / x86_64**. It does
not change Chaquopy's runtime support claims: upstream Chaquopy 17 already supports
Python 3.14. Python 3.15 and free-threaded Python are outside this stable lane.

Hermes currently avoids newer OpenAI/Pydantic dependencies and uses raising
Anthropic/FAL placeholders because its embedded wheel set is incomplete. The
work here builds the missing wheels and tests real library behavior before any
downstream Hermes dependency or stub is changed.

## Locked inputs

`sources.lock.json` binds official PyPI source archives by exact version, byte
count, and SHA-256. `prepare_sources.py` verifies those bytes before extraction,
rejects links and unsafe paths, and records the precise build-requirement-only
patch which pins Maturin to 1.15.0. It never retags a host wheel as Android.

| Package | Selected version | Treatment |
| --- | --- | --- |
| Jiter | 0.16.0 | Genuine Rust/PyO3 Android extension |
| Pydantic Core | 2.46.5 | Genuine Android extension; paired with Pydantic 2.13.5 |
| msgpack | 1.2.2 | Upstream `MSGPACK_PUREPYTHON=1` build |
| aiohttp | 3.14.3 | Official Android wheels from PyPI |
| OpenAI | 2.54.0 | Genuine SDK; bounded compatibility target, not latest-major claim |
| Anthropic | 0.125.0 | Genuine SDK; bounded compatibility target |
| FAL client | 1.0.1 | Genuine SDK with the msgpack fallback |

## Local preparation and tests

The helper tests run on Windows, Linux, or macOS; only the small `packaging`
library is needed for dependency-marker validation:

```sh
python -m pip install packaging==26.3
python -m unittest discover -s compat/hermes/tests -v
python compat/hermes/prepare_sources.py prepare --package jiter --dest /tmp/jiter-source
```

Android wheel builds require Linux/macOS, an Android SDK, Java, and Rust 1.88.0.
Use a new output directory for each invocation:

```sh
python -m pip install -r compat/hermes/requirements-build.txt
rustup toolchain install 1.88.0 --profile minimal
rustup default 1.88.0
python compat/hermes/build_native.py \
  --package jiter --python 3.14 --abi x86_64 \
  --source-cache /tmp/hermes-jiter-cp314 \
  --output .artifacts/jiter-cp314-x86_64
```

Repeat for `pydantic-core`, Python 3.13, and arm64-v8a as needed. The driver uses
pinned cibuildwheel, the Android API-24 target, Cargo's existing lockfile, and
16-KB linker alignment. It also clears ambient cibuildwheel skip settings so a
maintainer's environment cannot silently replace the configured runtime tests.

## Evidence gates

`.github/workflows/hermes-compat.yml` runs a separate job for each native
package/Python/ABI combination. It publishes wheels and JSON evidence as Actions
artifacts, not Git blobs. An artifact from a failed run is diagnostic only.

`audit_wheels.py` checks canonical ZIP layout, complete RECORD hashes, wheel
identity/tags, target ELF architecture, actual CPython initializer exports,
DT_NEEDED closure, and PT_LOAD alignment. **This is static evidence only.**

`runtime_smoke.py --require-android` must actually run on Android, import genuine
native modules, and pass every selected behavior test. It never turns a host
test into Android proof. SDK tests use offline HTTP fixtures; aiohttp tests use
loopback sockets. No real credentials or paid inference calls are needed.

cibuildwheel tests the architecture of the host runner. Its managed `minVersion`
testbed currently runs **API 27**, which is distinct from the wheel's **API 24**
build target. The native driver captures the actual test report from Android,
verifies the complete selected suite and genuine package inventory, and binds
the report's digest, measured API, Python version, and page size to its build
receipt. A successful compiler exit alone cannot certify runtime compatibility.
The device API comes from `platform.android_ver()`; Python's build-time minimum
from `sys.getandroidapilevel()` is retained separately, never substituted for it.

Therefore an arm64-v8a
wheel cross-built on an x86_64 runner is explicitly labelled
`not_run_cross_architecture`; it still needs an ARM64 device run. Static 16-KB
alignment likewise does not replace a real 16-KB-page runtime test.

The standalone app under `android/` uses a separate package name and does not
install over Hermes. Its hosted matrix tests both Python versions on **API 24
and API 35** x86_64 emulators. Each job installs only the assembled, audited
offline wheelhouse, then verifies every expected full-stack test and package
against the report extracted from that app. It also checks the measured API
against the matrix entry; a newer emulator cannot stand in for the API-24 gate.
Only passing, retained reports constitute runtime evidence for those targets.

Production Hermes dependency pins, stubs, MCP lifecycle
boundaries, signing keys, and release workflows remain unchanged by this lab.

## Upstream authority

- [Chaquopy version matrix](https://chaquo.com/chaquopy/doc/current/versions.html)
- [Chaquopy modern-wheel guidance](../../server/pypi/README.md)
- [cibuildwheel Android platform](https://cibuildwheel.pypa.io/en/stable/platforms/#android)

Actions artifacts are temporary development evidence, not a permanent package
index. Publish a versioned, checksum-bound wheel distribution only after the
required runtime matrix passes.
