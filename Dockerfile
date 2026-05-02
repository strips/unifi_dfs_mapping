FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY fjord_radar ./fjord_radar

ENV FJORD_CONFIG=/app/config/config.yaml \
    FJORD_DATA_DIR=/data \
    FJORD_LOG_LEVEL=INFO

EXPOSE 5514/udp 8080/tcp
VOLUME ["/data"]

# Note: container runs as the host UID/GID via docker-compose's `user:`
# directive, so files in the bind-mounted /data are owned by your user
# on the host. We do NOT bake a hard-coded UID into the image.

CMD ["python", "-m", "fjord_radar.app"]
