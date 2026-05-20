FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Warsaw

WORKDIR /app

COPY pyproject.toml README.md ./
COPY cipereusz-premium ./cipereusz-premium

RUN pip install --upgrade pip && pip install .

VOLUME ["/app/data"]

CMD ["python", "-m", "cipereusz-premium"]
