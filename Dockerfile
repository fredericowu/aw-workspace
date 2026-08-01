# aw-workspace — imagem slim, data plane de um único workspace.
# Sem CLIs de agente, sem docker socket, sem build de frontend.
FROM python:3.12-slim

ARG AW_WORKSPACE_VERSION=dev

# procps → `ps`, used by the terminal /procs + /kill endpoints (process badge).
# git → the baked-in repo (COPY . below, including .git) is meant to be
# worked on from inside the container, not just read.
# sudo → the `ubuntu` user is unprivileged by default (see USER below); sudo
# lets terminal sessions install packages / touch root-owned paths on demand
# instead of everyone needing `docker exec -u root`. Frederico decision 2026-08-01.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl build-essential libpq-dev procps git sudo \
    && rm -rf /var/lib/apt/lists/*

# `ubuntu` user (UID/GID 1001, standard Ubuntu first-user convention) — this
# is now the container's DEFAULT user (see `USER ubuntu` below), not just an
# opt-in option. Every process (the app itself, `docker exec`/terminal
# logins, PTY subprocesses spawned by the terminal feature) runs as this
# user. Frederico decision 2026-08-01 (supersedes the root-by-default
# 2026-07-28 decision).
RUN groupadd -g 1001 ubuntu && useradd -u 1001 -g 1001 -m -s /bin/bash ubuntu \
    && echo "ubuntu ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/ubuntu \
    && chmod 0440 /etc/sudoers.d/ubuntu

# The aw-workspace runtime lives at /opt/aw-workspace (not /app, and not the
# monolith's /opt/agentic-workspace). On a BYOD host this same path is
# bind-mounted from a host dir (~/aw-workspace) so it's visible/editable from
# the host and survives container recreation — see aw-remote-host's
# bootstrap/workspace/install.sh.
WORKDIR /opt/aw-workspace

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Full checkout (including .git — see the `fetch-depth: 0` checkout in
# build-image.yml) baked into the image so a first-boot host seed (see
# aw-remote-host's install.sh) starts from a real, working git repo — not
# just a source-code snapshot — and survives via the host bind-mount from
# then on. Frederico decision 2026-08-01.
COPY . /opt/aw-workspace
RUN chown -R ubuntu:ubuntu /opt/aw-workspace \
    && git config --system --add safe.directory /opt/aw-workspace

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
#
# /opt/aw-workspace/bin holds this repo's OWN `aw-workspace` CLI (see
# skills/aw-workspace/SKILL.md) — on PATH so it's callable from any cwd/shell
# (including agent sessions) without `./bin/` prefixing.
ENV PATH="/opt/aw-workspace/bin:/opt/aw-workspace/.aw-workspace/bin:${PATH}"

EXPOSE 9030

HEALTHCHECK --interval=10s --timeout=5s --retries=3 \
    CMD curl -fsS http://localhost:9030/api/health || exit 1

# Default user for PID 1 AND for `docker/podman exec` (no `-u` flag needed to
# log in as ubuntu) — every child process (PTY terminal sessions, etc.)
# inherits it. Must come last: everything above needs root (apt-get, chown).
USER ubuntu

CMD ["python", "-m", "src.start.workspace"]
