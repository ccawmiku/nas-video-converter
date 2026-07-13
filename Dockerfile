FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.source="https://github.com/ccawmiku/nas-video-converter" \
      org.opencontainers.image.title="NAS Video Converter" \
      org.opencontainers.image.description="Safe, plan-first NAS video inspection and conversion service" \
      org.opencontainers.image.licenses="MIT"

ARG TARGETARCH
RUN test "$TARGETARCH" = "amd64" || (echo "Only linux/amd64 is supported" >&2; exit 1)

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MEDIA_ROOT=/media \
    CONFIG_DIR=/config \
    TZ=Asia/Shanghai \
    PUID=1026 \
    PGID=100 \
    QSV_DEVICE=/dev/dri/renderD128 \
    LIBVA_DRIVER_NAME=iHD

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg gosu tini tzdata intel-media-va-driver \
    && apt-get clean

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY scripts/docker-entrypoint.sh /usr/local/bin/nvc-entrypoint
RUN chmod 0755 /usr/local/bin/nvc-entrypoint && mkdir -p /media /config

EXPOSE 12012
VOLUME ["/config"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:12012/health', timeout=3)"

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/nvc-entrypoint"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "12012", "--workers", "1"]
