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

# System dependencies for numpy/scipy
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libgomp1 \
 && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir \
    numpy">=1.24,<2.0" \
    scipy">=1.11" \
    pandas">=2.0" \
    scikit-learn">=1.3" \
    joblib">=1.3"
# matplotlib is NOT installed — --no-plot is always used inside Docker

# Copy gateway source
COPY recv_verify.py .
COPY bearing_math.py .

# Satellite TCP port
EXPOSE 5100
# Dashboard HTTP port
EXPOSE 8080

# Use environment variables for runtime config so the image stays generic
ENV FACTORY_NAME="EPM Factory"
ENV AUTH=""
ENV NOTIFY_WEBHOOK=""
ENV NOTIFY_EMAIL=""
ENV GATEWAY_PORT=5100
ENV DASHBOARD_PORT=8080

ENTRYPOINT ["sh", "-c", \
  "python3 recv_verify.py \
    --no-plot \
    --factory-name \"$FACTORY_NAME\" \
    $([ -n \"$AUTH\" ] && echo \"--auth $AUTH\") \
    $([ -n \"$NOTIFY_WEBHOOK\" ] && echo \"--notify-webhook $NOTIFY_WEBHOOK\") \
    $([ -n \"$NOTIFY_EMAIL\" ] && echo \"--notify-email $NOTIFY_EMAIL\") \
    --port $GATEWAY_PORT \
    --dashboard-port $DASHBOARD_PORT"]
