FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    RELOAD=0 \
    HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential python3-dev libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml poetry.lock ./

RUN pip install --upgrade pip \
    && pip install --no-cache-dir \
        "fastapi>=0.111.0" \
        "uvicorn[standard]>=0.30.1" \
        "httpx>=0.27.0" \
        "beancount>=2.3.5" \
        "fava>=1.26" \
        "pydantic>=2.7.0" \
        "python-dotenv>=1.0.1" \
        "aiosqlite>=0.20.0"

COPY app ./app
COPY data ./data

EXPOSE 8000 5001

CMD ["python", "-m", "app.cli"]
