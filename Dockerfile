FROM python:3.12-slim

LABEL org.opencontainers.image.title="Rclone S3 Management UI" \
      org.opencontainers.image.description="Interactive rclone UI for backup, restore, and data transfer" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Pinned so an image rebuild is reproducible. Bump deliberately: a new rclone can
# change flag support, and the operation registry in app/operations.py is
# verified against the installed version.
#
# NOTE: do not name this ARG `RCLONE_VERSION` — rclone maps every
# `RCLONE_<FLAG>` environment variable to the corresponding flag, so
# RCLONE_VERSION="1.75.0" is parsed as `--version=1.75.0` (a bool) and every
# rclone call inside the build fails.
ARG RCLONE_PINNED_VERSION=1.75.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN curl -sSL "https://downloads.rclone.org/v${RCLONE_PINNED_VERSION}/rclone-v${RCLONE_PINNED_VERSION}-linux-amd64.zip" -o /tmp/rclone.zip \
    && unzip -q /tmp/rclone.zip -d /tmp/rclone \
    && cp "/tmp/rclone/rclone-v${RCLONE_PINNED_VERSION}-linux-amd64/rclone" /usr/local/bin/rclone \
    && chmod +x /usr/local/bin/rclone \
    && rclone version \
    && rm -rf /tmp/rclone /tmp/rclone.zip

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY entrypoint.sh /usr/local/bin/entrypoint.sh

RUN chmod +x /usr/local/bin/entrypoint.sh \
    && useradd --create-home --uid 1000 --shell /usr/sbin/nologin appuser \
    && mkdir -p /app/data/logs \
    && chown -R appuser:appuser /app

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
