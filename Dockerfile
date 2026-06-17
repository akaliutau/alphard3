FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MPLCONFIGDIR=/tmp/matplotlib \
    ENV_FILE=.env.cloud \
    DATA_DIR=data \
    IMAGE_CACHE_DIR=img_cache \
    SQLITE_PATH=data/alphard.sqlite3

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        fontconfig \
        libfreetype6 \
        libpng16-16 \
        build-essential \
        gcc \
        g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install -r requirements.txt

COPY . .

RUN mkdir -p /app/data /app/img_cache /tmp/matplotlib \
    && useradd --uid 10001 --gid 0 --home-dir /app --no-create-home --shell /usr/sbin/nologin alphard \
    && chown -R 10001:0 /app /tmp/matplotlib \
    && chmod -R g=u /app /tmp/matplotlib

USER 10001

ENTRYPOINT ["python", "app.py"]
CMD ["--once"]
