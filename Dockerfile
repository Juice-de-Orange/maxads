# One image for both processes: the web service and the media worker.
FROM python:3.13-slim

# ffmpeg/ffprobe do the video work; the web service reports them in /healthz.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --system --uid 1000 --home-dir /app --shell /usr/sbin/nologin maxads

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

# /data is the volume for originals, media, database and logs; /run/maxads holds
# the wake-up pipe between web and worker (a shared tmpfs in docker-compose.yml).
# Created here so that fresh named volumes inherit the ownership.
RUN mkdir -p /data /run/maxads && chown maxads:maxads /data /run/maxads

ENV MAXADS_BASE=/data \
    MAXADS_REQUIRE_MOUNT=0 \
    RUNTIME_DIRECTORY=/run/maxads \
    PYTHONUNBUFFERED=1

USER maxads
EXPOSE 8080
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", \
     "--no-access-log", "--timeout-keep-alive", "15"]
