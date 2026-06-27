# EPM Gateway — Docker deployment for Arduino Uno Q
#
# Build:
#   docker build -t epm-gateway .
#
# Run (replace values as needed):
#   docker run -d --name epm \
#     --network host \
#     -v $(pwd)/logs:/app/logs \
#     -v $(pwd)/model:/app/model \
#     -e FACTORY_NAME="Plant A - Line 3" \
#     -e AUTH="admin:yourpassword" \
#     -e NOTIFY_WEBHOOK="https://hooks.slack.com/services/..." \
#     epm-gateway
#
# Stop:
#   docker stop epm && docker rm epm
#
# View logs:
#   docker logs -f epm

FROM python:3.11-slim-bookworm

WORKDIR /app

# System dependencies: gcc for numpy, libgomp for scikit-learn parallel,
# libstdc++ for TFLite shared libraries on aarch64 (Arduino Uno Q).
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libgomp1 \
    libstdc++6 \
    wget \
 && rm -rf /var/lib/apt/lists/*

# ── Python ML dependencies ────────────────────────────────────────────────────
# Install in order: numpy first (many packages depend on it),
# then TFLite runtime, then the rest.
COPY requirements.txt .
RUN pip install --no-cache-dir \
    "numpy>=1.24,<2.0" \
    "scipy>=1.11" \
    "pandas>=2.0" \
    "scikit-learn>=1.3" \
    "joblib>=1.3"

# TensorFlow Lite runtime — small (no training), ARM64-compatible.
# On Uno Q (aarch64) this is the inference-only runtime; training uses
# tensorflow-cpu (heavier, installed separately below).
# tflite-runtime wheel:  ~2 MB   vs  tensorflow-cpu ~200 MB
RUN pip install --no-cache-dir tflite-runtime || \
    echo "[Docker] tflite-runtime not available for this arch — will use tensorflow.lite"

# TensorFlow CPU — required for training (autoencoder.py).
# Skip on very low-memory targets by setting BUILD_TRAINING=0 in .env.
ARG BUILD_TRAINING=1
RUN if [ "$BUILD_TRAINING" = "1" ]; then \
      pip install --no-cache-dir "tensorflow-cpu>=2.13,<2.16"; \
    fi

# ── Qualcomm NPU delegate note ────────────────────────────────────────────────
# libQnnHtp.so (Hexagon 770 NPU) is provided by the Qualcomm AI Engine Direct
# SDK, which must be installed on the HOST Uno Q before running the container.
# The container mounts it via docker-compose volumes or --device passthrough.
# When available, NpuInferencer (autoencoder.py) auto-detects and uses it.
# If missing, inference falls back to CPU TFLite automatically.

# matplotlib is NOT installed — --no-plot is always used inside Docker
# Copy gateway source
COPY recv_verify.py .
COPY bearing_math.py .
COPY docker_start.py .
COPY ml_trainer.py .
COPY autoencoder.py .

# Satellite TCP port
EXPOSE 5100
# Dashboard HTTP port
EXPOSE 8080

# Use environment variables for runtime config so the image stays generic.
# docker_start.py reads these and passes them as Python list args — safe with
# spaces in FACTORY_NAME or passwords (no shell word-splitting).
ENV FACTORY_NAME="EPM Factory"
ENV AUTH=""
ENV NOTIFY_WEBHOOK=""
ENV NOTIFY_EMAIL=""
ENV GATEWAY_PORT=5100
ENV DASHBOARD_PORT=8080

ENTRYPOINT ["python3", "docker_start.py"]
