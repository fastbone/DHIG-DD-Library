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

# rclone mirrors connected SharePoint libraries. It runs as the unprivileged user,
# needs no capabilities, and writes only under /data — its configuration arrives
# entirely through the environment, so there is no config file for it to own.
#
# Pinned upstream, deliberately not Debian's package: app-only auth for
# SharePoint (`client_credentials`) landed in rclone 1.69.0, and trixie ships
# 1.60.1, which cannot authenticate without a browser at all — so `apt install
# rclone` would build an image where every sync fails on credentials. Checked
# against the published SHA256SUMS; unzipped with python3 rather than adding
# `unzip` to the image.
ARG RCLONE_VERSION=1.75.0
RUN set -eux; \
    case "$(dpkg --print-architecture)" in \
      amd64) rarch=amd64 ;; \
      arm64) rarch=arm64 ;; \
      *) echo "no rclone build for $(dpkg --print-architecture)" >&2; exit 1 ;; \
    esac; \
    base="rclone-v${RCLONE_VERSION}-linux-${rarch}"; \
    cd /tmp; \
    curl -fsSL -o rclone.zip "https://downloads.rclone.org/v${RCLONE_VERSION}/${base}.zip"; \
    curl -fsSL -o SHA256SUMS "https://downloads.rclone.org/v${RCLONE_VERSION}/SHA256SUMS"; \
    grep " ${base}.zip\$" SHA256SUMS | sed "s# ${base}.zip# rclone.zip#" | sha256sum -c -; \
    python3 -c "import zipfile; zipfile.ZipFile('rclone.zip').extractall('.')"; \
    install -m 0755 "${base}/rclone" /usr/local/bin/rclone; \
    rm -rf rclone.zip SHA256SUMS "${base}"; \
    rclone version | head -1

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
RUN groupadd --system --gid 10001 dd \
 && useradd --system --uid 10001 --gid 10001 --create-home --home-dir /home/dd dd \
 && mkdir -p /data /corpus \
 && chown -R dd:dd /data \
 && chmod -R a-w /app

USER dd

VOLUME ["/data", "/corpus"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

CMD ["python", "-m", "app.server"]
