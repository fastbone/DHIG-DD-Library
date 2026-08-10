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
RUN useradd --system --uid 10001 --create-home --home-dir /home/dd dd \
 && mkdir -p /data /corpus \
 && chown -R dd:dd /data \
 && chmod -R a-w /app

USER dd

VOLUME ["/data", "/corpus"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

CMD ["python", "-m", "app.server"]
