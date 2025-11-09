FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PDM_CHECK_UPDATE=0 \
    RELOAD=0 \
    HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app

# Install system dependencies and PDM
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential python3-dev libffi-dev curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --upgrade pip \
    && pip install pdm

# Copy dependency files
COPY pyproject.toml pdm.lock* ./

# Install dependencies only (without dev dependencies)
RUN pdm install --prod --no-lock --no-editable

# Copy application code and README
COPY app ./app
COPY data ./data
COPY README.md ./

# Install the application itself
RUN pdm install --prod --no-lock

EXPOSE 8000 5001

CMD ["pdm", "run", "runserver"]
