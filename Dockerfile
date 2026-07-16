FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Warsaw

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libopus0 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY cipereusz_premium ./cipereusz_premium

RUN pip install --upgrade pip && pip install .

VOLUME ["/app/data"]

CMD ["python", "-m", "cipereusz_premium"]