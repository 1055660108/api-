FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium

COPY app ./app
COPY VERSION ./VERSION
COPY schema ./schema
COPY scripts/storage_migrate.py ./scripts/storage_migrate.py
COPY run.py worker.py config.example.json ./

ARG MIHOMO_URL=https://github.com/MetaCubeX/mihomo/releases/download/v1.19.29/mihomo-linux-amd64-compatible-v1.19.29.gz
ARG MIHOMO_SHA256=5612e698e96c8b8ad15abc4c0a4f098eba9234354b4f248cb97f2528e215b094
RUN mkdir -p /app/bin \
    && MIHOMO_URL="$MIHOMO_URL" MIHOMO_SHA256="$MIHOMO_SHA256" python -c "import gzip,hashlib,os,urllib.request; data=urllib.request.urlopen(os.environ['MIHOMO_URL'], timeout=60).read(); assert hashlib.sha256(data).hexdigest() == os.environ['MIHOMO_SHA256']; open('/app/bin/mihomo','wb').write(gzip.decompress(data))" \
    && chmod 755 /app/bin/mihomo

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /var/lib/dola-fetch-service \
    && chown -R appuser:appuser /app /var/lib/dola-fetch-service /ms-playwright

USER appuser

EXPOSE 8088

CMD ["python", "run.py"]
