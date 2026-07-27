# aw-workspace — imagem slim, data plane de um único workspace.
# Sem CLIs de agente, sem docker socket, sem build de frontend.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY . /app

# Single worker: the terminal feature keeps PTY sessions in-process memory, so
# create/WS must land on the same worker. A single-user data-plane doesn't need
# more. Revisit if a stateless multi-worker backing store is added (MIGRATION.md).
ENV AW_PORT=9030 \
    AW_WORKSPACE_WORKERS=1 \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1

EXPOSE 9030

HEALTHCHECK --interval=10s --timeout=5s --retries=3 \
    CMD curl -fsS http://localhost:9030/api/health || exit 1

CMD ["python", "-m", "src.start.workspace"]
