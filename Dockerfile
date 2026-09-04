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
# unzip → apps ship release archives (terraform, awscli v2) and their
# installers used to abort with "unsupported base image" on every boot for the
# want of it. Those now fall back to python3's stdlib zipfile so they work on
# any base, but a 200KB package is cheaper than every future app rediscovering
# this — and `unzip` in a terminal is table stakes.
# screen → W5 restored the GNU screen backing for terminal sessions. A PTY's
# master fd belongs to the process that forked it, so a screen server — which
# is external to every uvicorn worker — is what lets any worker serve
# /ws/terminal for any session, and what lets a session outlive a restart.
# Without this binary src/api/terminal_manager.py falls back to a direct PTY
# and terminals are worker-owned again (silently, and correctly, at
# AW_WORKSPACE_WORKERS=1).
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl build-essential libpq-dev procps git sudo unzip screen \
    && rm -rf /var/lib/apt/lists/*

# Headless-Chromium shared libraries. This image has no GUI stack, so a
# Chromium downloaded at runtime (`playwright install chromium`, which
# aw-app-presentations does lazily on its first PNG export) lands fine and then
# dies on launch with "Target page, context or browser has been closed" — an
# error that names nothing. `ldd` on the binary told the real story: 17 "not
# found" entries. Found 2026-08-14 when export was fixed end-to-end.
#
# Baked into the image rather than left to the app's `--with-deps`, which
# apt-installs at runtime: that costs ~30s on the first export of every fresh
# workspace, needs sudo from an unprivileged process, and silently depends on
# the container having a working apt index — three ways to fail for something
# that is the same on every build. The app keeps its runtime install as the
# fallback for workspaces still on an older image.
#
# Package names verified against this exact base (Debian 13 trixie) rather than
# copied from playwright's docs — libasound2 in particular is libasound2t64 on
# some releases, and one wrong name fails the whole image build.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
        libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
        libgbm1 libpango-1.0-0 libcairo2 libasound2 libglib2.0-0 \
        libdbus-1-3 libatspi2.0-0 libxcb1 libx11-6 libxext6 libxi6 \
        fonts-liberation \
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

# Installed into a venv under the host-bind-mounted tree (not the system
# site-packages) so this workspace's own process AND every sibling CLI-agent
# runner container (aw-app-agents-platform-runners' execute.py, which mounts
# this same tree read-write into a DIFFERENT base image) can resolve the same
# packages via one shared location instead of drifting. Frederico decision
# 2026-08-05: cross-image binary sharing (the venv's own interpreter, any
# compiled C-extension wheel like psycopg[binary]/cryptography) is NOT safe
# across different base images/distros — only PYTHONPATH-import a venv's
# site-packages from a sibling, never its bin/python. See execute.py's own
# comment at the PYTHONPATH assignment for the runner side of this.
RUN python3 -m venv /opt/aw-workspace/.aw-workspace/venv \
    && /opt/aw-workspace/.aw-workspace/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt \
    && sha256sum /tmp/requirements.txt | awk '{print $1}' \
       > /opt/aw-workspace/.aw-workspace/venv/.requirements.sha256

# Full checkout (including .git — see the `fetch-depth: 0` checkout in
# build-image.yml) baked into the image so a first-boot host seed (see
# aw-remote-host's install.sh) starts from a real, working git repo — not
# just a source-code snapshot — and survives via the host bind-mount from
# then on. Frederico decision 2026-08-01.
COPY . /opt/aw-workspace
RUN chown -R ubuntu:ubuntu /opt/aw-workspace \
    && git config --system --add safe.directory /opt/aw-workspace

# 10 workers (W0-W6 multiworker chain): the boot path, periodic singletons,
# per-app lifecycle, WS registries and terminal PTYs were all made
# multi-worker-safe first (see W1-W5) — the state that used to be in-process
# memory now lives in Postgres/Redis/GNU screen, so any worker can serve any
# request. See MIGRATION.md for the phase-by-phase history.
# HOME is left at its useradd default (/home/ubuntu) — overriding it to
# /opt/aw-workspace was confusing (a fresh terminal opening in the workspace
# root is a nice-to-have, not worth hijacking $HOME for). AW_WORKSPACE_HOME is
# set explicitly instead: it's what paths.py's workspace_home() actually reads
# for F4 app state (bin shims, secrets, skills), and it must keep pointing at
# /opt/aw-workspace/.aw-workspace to match the hardcoded PATH entry below and
# the existing host bind-mount — decoupled from $HOME on purpose.
ENV AW_PORT=9030 \
    AW_WORKSPACE_WORKERS=10 \
    AW_WORKSPACE_VERSION=${AW_WORKSPACE_VERSION} \
    PYTHONPATH=/opt/aw-workspace \
    AW_WORKSPACE_HOME=/opt/aw-workspace/.aw-workspace \
    PYTHONUNBUFFERED=1

# F4 app-shim bin dir (paths.bin_dir() == <workspace_home>/bin, i.e.
# $AW_WORKSPACE_HOME/bin with the AW_WORKSPACE_HOME above) baked onto PATH for
# every process/shell/agent in this image — not just login shells (the
# orchestrator's /etc/profile.d/aw-bin.sh workaround only covered those).
# Lives under the host bind-mount, so installed shims persist across
# container recreation; this ENV is what finally makes paths.py's
# long-standing "on PATH" claim true. Frederico decision 2026-07-28.
#
# This repo's OWN `aw-workspace-cli` CLI (see skills/aw-workspace/SKILL.md)
# lives at the repo root (./aw-workspace-cli, which is also WORKDIR above) —
# putting the repo root itself on PATH is what makes the bare
# `aw-workspace-cli` form work on PATH from any cwd/shell (including agent
# sessions), no bin/ dir or symlink needed.
#
# venv/bin goes FIRST: `python`/`pip` (and aw-workspace-cli's own
# `#!/usr/bin/env python3` shebang) resolve to the venv here, inside THIS
# image only — never prepend this to PATH in a different base image/container
# (see the venv RUN step's comment above).
ENV PATH="/opt/aw-workspace/.aw-workspace/venv/bin:/opt/aw-workspace:/opt/aw-workspace/.aw-workspace/bin:${PATH}"

# ...and the same three entries AGAIN, for login shells, because the ENV above
# is not enough on its own: Debian's /etc/profile hard-ASSIGNS (not appends)
#     PATH="/usr/local/bin:/usr/bin:/bin:/usr/local/games:/usr/games"
# for every non-root user, then exports it — wiping the ENV inherited from the
# image. Terminal sessions are exactly that case: terminal_manager.py spawns
# `bash -lc '... exec bash -l'`, a login shell, so every terminal in the UI
# came up WITHOUT the workspace on PATH and a bare `aw-workspace-cli` was
# "command not found" (measured 2026-08-12; the note above about the
# orchestrator's profile.d file being merely a "workaround" the ENV replaced
# had it backwards — the two cover different shells and BOTH are needed).
#
# Idempotent + order-preserving: prepends in reverse so the final order matches
# the ENV, and skips an entry already present so re-sourcing a profile (su -,
# nested login shells) can't grow PATH without bound.
RUN cat > /etc/profile.d/aw-path.sh <<'AWPATH' \
    && chmod 0644 /etc/profile.d/aw-path.sh
# Re-prepend the aw-workspace PATH entries that /etc/profile just wiped.
# Managed by aw-workspace's Dockerfile — see the comment there before editing.
for _aw_dir in /opt/aw-workspace/.aw-workspace/bin /opt/aw-workspace /opt/aw-workspace/.aw-workspace/venv/bin; do
    case ":${PATH}:" in
        *":${_aw_dir}:"*) ;;
        *) PATH="${_aw_dir}:${PATH}" ;;
    esac
done
unset _aw_dir
export PATH
AWPATH

EXPOSE 9030

HEALTHCHECK --interval=10s --timeout=5s --retries=3 \
    CMD curl -fsS http://localhost:9030/api/health || exit 1

# Default user for PID 1 AND for `docker/podman exec` (no `-u` flag needed to
# log in as ubuntu) — every child process (PTY terminal sessions, etc.)
# inherits it. Must come last: everything above needs root (apt-get, chown).
USER ubuntu

# Boot under the image's BASE interpreter via an ABSOLUTE path (bypasses the
# venv-first PATH above on purpose): PID 1 must start even when the persistent
# mount's venv is missing or its bin/python is a dangling symlink, so
# src.start.workspace can (re)build the venv on the mount and only THEN
# os.execv into it (_reexec_into_venv). Using bare `python` here would exec the
# venv's interpreter and deadlock boot whenever that venv is absent/broken.
CMD ["/usr/local/bin/python3", "-m", "src.start.workspace"]
