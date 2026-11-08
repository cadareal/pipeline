# Imagem do Cloud Run Job. Build a partir da raiz do repositório:
#   docker build -f infra/Dockerfile -t recibo .
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config

RUN uv sync --no-dev --extra gcp

ENV RECIBO_STORE=gcs \
    RECIBO_DATA_DIR=/tmp/recibo \
    RECIBO_CONFIG_DIR=/app/config \
    PATH="/app/.venv/bin:$PATH"

ENTRYPOINT ["recibo"]
CMD ["run"]
