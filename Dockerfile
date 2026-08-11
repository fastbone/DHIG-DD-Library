FROM python:3.12-slim AS base

# INSTALL_OCR=true adds Tesseract (~250 MB) so scanned PDFs can be OCR'd.
ARG INSTALL_OCR=false

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DD_DATA_DIR=/data \
    DD_HOST=0.0.0.0 \
    DD_PORT=8000

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      curl \
      libgl1 \
      libglib2.0-0 \
 && if [ "$INSTALL_OCR" = "true" ]; then \
      apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng tesseract-ocr-deu; \
    fi \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY web/ ./web/
COPY tools/ ./tools/
COPY README.md ./

# Unprivileged runtime user. run_python executes model-authored code as this
# user, so it must own as little as possible: /app stays root-owned and
# read-only to the app, only /data is writable.
#
# The gid is pinned as well as the uid: docker-compose.yml mounts a tmpfs over
# /home/dd with uid/gid 10001, and a tmpfs is root-owned unless told otherwise.
# /inbox is the drop point for external feeds, and its ownership here is load
# bearing. Docker seeds a *new* named volume from whatever the image has at the
# mount path, ownership and mode included — so creating it as dd:dd 2775 is what
# makes the volume arrive writable by the runtime user instead of root-owned.
# The setgid bit means files a feed creates inside inherit group 10001 even when
# it runs as another uid, which is half of what the app needs to read them (the
# other half is the feed's umask; see the README).
#
# This only applies the first time the volume is created. An inbox volume that
# already exists keeps whatever ownership it has.
RUN groupadd --system --gid 10001 dd \
 && useradd --system --uid 10001 --gid 10001 --create-home --home-dir /home/dd dd \
 && mkdir -p /data /corpus /inbox \
 && chown -R dd:dd /data /inbox \
 && chmod 2775 /inbox \
 && chmod -R a-w /app

USER dd

VOLUME ["/data", "/corpus", "/inbox"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

CMD ["python", "-m", "app.server"]
