# prack for a small server: docker build -t prack . && docker run -p 127.0.0.1:8000:8000 -v prack-data:/data prack
# (docker-compose.yml does the same, with a volume, a restart policy and optional HTTPS.)

FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY prack ./prack
# PostgreSQL support is included (psycopg ships its own libpq); SQLite needs nothing extra.
RUN pip install --no-cache-dir --prefix=/install ".[postgres]"

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PRACK_HOST=0.0.0.0 \
    PRACK_PORT=8000 \
    PRACK_DATA_DIR=/data
RUN useradd --system --uid 10001 --home-dir /data --shell /usr/sbin/nologin prack \
    && mkdir /data && chown prack:prack /data
COPY --from=build /install /usr/local
USER prack
WORKDIR /data
VOLUME /data
EXPOSE 8000
# /api/health never asks for a password.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('PRACK_PORT', '8000'), timeout=4)"]
ENTRYPOINT ["prack"]
CMD ["run"]
