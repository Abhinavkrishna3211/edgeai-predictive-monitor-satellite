# Contributing

## Dev environment

**Gateway (Python, does the actual analytics/testing work):**
```bash
python3 -m venv venv
source venv/bin/activate   # or venv\Scripts\activate on Windows
pip install -r mic_tools/requirements.txt -r mic_tools/requirements-dev.txt
```

**Firmware (ESP32-S3, only needed if you're touching `src/`/`components/`):**
[PlatformIO](https://platformio.org/) with the `espidf` framework — see the
Quick Start sections in [README.md](README.md) for board setup and flashing.

## Running the tests

```bash
# Gateway pytest suite (what CI runs)
python -m pytest tests/ -q

# Firmware host-side regression tests (native gcc/CMake, no ESP-IDF/hardware needed)
cmake -S tests/host -B tests/host/build
cmake --build tests/host/build
ctest --test-dir tests/host/build --output-on-failure
```

See [tests/host/README.md](tests/host/README.md) for what "pass" means there —
one check is an intentionally documented `EXPECTED-FAIL`, not a bug.

## Conventions

Naming, commit format, error-handling patterns, ADR process — all in
[docs/CONVENTIONS.md](docs/CONVENTIONS.md). Read it before your first PR.
