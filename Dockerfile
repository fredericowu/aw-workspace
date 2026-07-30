# aw-workspace — imagem slim, data plane de um único workspace.
# Sem CLIs de agente, sem docker socket, sem build de frontend.
FROM python:3.12-slim

ARG AW_WORKSPACE_VERSION=dev

# procps → `ps`, used by the terminal /procs + /kill endpoints (process badge).
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl build-essential libpq-dev procps \
    && rm -rf /var/lib/apt/lists/*

# `aw` user (UID/GID 1001, standard Ubuntu first-user convention) — created so
# a non-root option EXISTS, not used by default. The container still runs as
# root (no `USER aw` here); which apps opt into running as this user is a
# future per-app `run_as`-style manifest field, not implemented yet.
# Frederico decision 2026-07-28.
RUN groupadd -g 1001 aw && useradd -u 1001 -g 1001 -m -s /bin/bash aw

# The aw-workspace runtime lives at /opt/aw-workspace (not /app, and not the
# monolith's /opt/agentic-workspace). On a BYOD host this same path is
# bind-mounted from a host dir (~/aw-workspace) so it's visible/editable from
# the host and survives container recreation — see aw-remote-host's
# bootstrap/workspace/install.sh.
WORKDIR /opt/aw-workspace

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY . /opt/aw-workspace

# Single worker: the terminal feature keeps PTY sessions in-process memory, so
# create/WS must land on the same worker. A single-user data-plane doesn't need
# more. Revisit if a stateless multi-worker backing store is added (MIGRATION.md).
# HOME=/opt/aw-workspace so a fresh terminal (Agents → Terminals) opens in
# the workspace root — terminal_manager falls back to $HOME when no cwd given.
ENV AW_PORT=9030 \
    AW_WORKSPACE_WORKERS=1 \
    AW_WORKSPACE_VERSION=${AW_WORKSPACE_VERSION} \
    PYTHONPATH=/opt/aw-workspace \
    HOME=/opt/aw-workspace \
    PYTHONUNBUFFERED=1

# F4 app-shim bin dir (paths.bin_dir() == <workspace_home>/bin, i.e.
# $HOME/.aw-workspace/bin with the HOME above) baked onto PATH for every
# process/shell/agent in this image — not just login shells (the
# orchestrator's /etc/profile.d/aw-bin.sh workaround only covered those).
# Lives under the host bind-mount, so installed shims persist across
# container recreation; this ENV is what finally makes paths.py's
# long-standing "on PATH" claim true. Frederico decision 2026-07-28.
ENV PATH="/opt/aw-workspace/.aw-workspace/bin:${PATH}"

EXPOSE 9030

HEALTHCHECK --interval=10s --timeout=5s --retries=3 \
    CMD curl -fsS http://localhost:9030/api/health || exit 1

CMD ["python", "-m", "src.start.workspace"]
