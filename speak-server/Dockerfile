FROM python:3.12-alpine

# pulseaudio-utils gives paplay (playback) and pactl (sink enumeration).
# tzdata is what zoneinfo reads, so QUIET_HOURS_TZ can name a real timezone
# instead of everything being UTC.
RUN apk add --no-cache pulseaudio-utils tzdata

# The only dependency, and only the MQTT bridge uses it. Build with
# --build-arg WITH_MQTT=0 for a dependency-free image; the server detects the
# missing library and logs a warning instead of failing.
ARG WITH_MQTT=1
RUN if [ "$WITH_MQTT" = "1" ]; then \
      pip install --no-cache-dir --root-user-action=ignore "paho-mqtt>=1.6,<3"; \
    fi

WORKDIR /app
COPY *.py /app/
COPY dashboard /app/dashboard

# Cache, history DB and clips. Mounted in compose; created here so the image
# works standalone, and owned by the desktop uid that the container runs as.
RUN mkdir -p /data && chown 1000:1000 /data
ENV DATA_DIR=/data

EXPOSE 8899

# Uses the stdlib rather than curl/wget, which this image doesn't carry.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8899/health', timeout=4).read().strip()==b'ok' else 1)"

CMD ["python", "-u", "/app/server.py"]
