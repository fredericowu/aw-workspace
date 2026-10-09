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
# NOT here: `screen`. W5 installed it to relay terminal sessions between
# uvicorn workers; W7 replaced that with two Redis Streams per session
# (src/api/terminal_manager.py), so nothing in this image shells out to it any
# more. `procps` stays — _ps_snapshot() needs `ps` for the process badge.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl build-essential libpq-dev procps git sudo unzip \
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

# System CLIs that apps install at runtime, baked here instead — the same
# trade Chromium's libraries above already make, for the same reason, and
# measured on the aw host 2026-10-09.
#
# Every workspace Update recreates the container, and these packages live in
# the container's writable layer, so each one was re-downloaded on the next
# boot. They do not merely cost their own time: CommandInstaller._run holds
# ONE global flock around every installer script in the workspace (concurrent
# apt/dpkg corrupts), so the installs are strictly serial and each sits on
# the critical path of every app queued behind it. The boot reconcile blew
# its 1200s budget on two consecutive boots because of this, leaving the Apps
# panel empty for 20+ minutes.
#
# Measured cost of what is baked here: ffmpeg alone took 311s (sampled live),
# and the rest ~30s each. Baking makes them free, because each app's
# installer already opens with a guard — `if command -v gh; then echo
# "already installed"; exit 0` — that could never fire while the binary was
# wiped on every recreate. The guards are unchanged and still the fallback
# for a workspace on an older image; this just lets them win.
#
# NOT baked, deliberately: awscli (133s), gcloud (210s) and docker. They are
# the expensive ones in IMAGE size — gcloud alone is ~1GB — so they are a
# size-vs-boot trade to decide with usage data, not a default. Their
# installers are untouched and keep working exactly as today.
#
# Package names verified against this exact base (python:3.12-slim = Debian
# 13 trixie) by running apt-cache in it, not copied from a shell's memory —
# the Chromium block above says why that matters.
RUN apt-get update && apt-get install -y --no-install-recommends \
        vim telnet netcat-openbsd iputils-ping rsync openssh-client ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# `gh` is not in Debian — it ships from GitHub's own apt repo, so baking it
# means reproducing the keyring + source list that aw-app-git's install_gh.sh
# does at runtime. Worth the extra layer: `gh` is used by the git app on
# essentially every workspace, and the runtime path pays an apt-get update
# against a third-party repo on every single container recreate.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates gpg \
    && mkdir -p -m 755 /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# nvm lives on the HOST MOUNT, and interactive shells have to be told so by
# the image — not by ~/.profile.
#
# src/apps/paths.py:nvm_dir() moves NVM_DIR under AW_WORKSPACE_HOME so nvm,
# node and every `npm install -g` package survive a container recreate (see
# that docstring for the 577s this cost per boot). That alone would have
# broken terminals in a way that takes a while to notice:
#
#   * nvm's own installer writes `export NVM_DIR=...` into ~/.profile, which
#     lives in the container layer and dies with every recreate.
#   * install_nvm.sh exits early when $NVM_DIR/nvm.sh already exists — and
#     once nvm is on the host mount it ALWAYS exists. So the installer would
#     never run again, and therefore never rewrite the profile.
#
# Net effect without this file: nvm persists perfectly and no interactive
# shell can find it. Putting the export in /etc/profile.d ties it to the
# IMAGE, which is rebuilt on every release, instead of to a dotfile that is
# deleted on every recreate.
#
# The fallback mirrors paths.py's own (DEFAULT_WORKSPACE_CONTAINER_DIR), so
# the two cannot drift apart silently; `.` is guarded because a workspace
# that has not installed nvm yet must still get a working shell.
RUN printf '%s\n' \
        'export NVM_DIR="${AW_WORKSPACE_HOME:-/opt/aw-workspace/.aw-workspace}/nvm"' \
        '[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"' \
        > /etc/profile.d/aw-nvm.sh \
    && chmod 0644 /etc/profile.d/aw-nvm.sh

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

# 10 workers (W0-W7 multiworker chain): the boot path, periodic singletons,
# per-app lifecycle, WS registries and terminal PTYs were all made
# multi-worker-safe first (see W1-W5) — the state that used to be in-process
# memory now lives in Postgres and Redis, so any worker can serve any request.
# A terminal PTY is the one thing still forked by exactly ONE worker (a master
# fd cannot cross a process boundary); since W7 its BYTES cross instead, over
# two Redis Streams per session. See MIGRATION.md for the phase-by-phase
# history.
# HOME is left at its useradd default (/home/ubuntu) — overriding it to
# /opt/aw-workspace was confusing (a fresh terminal opening in the workspace
# root is a nice-to-have, not worth hijacking $HOME for). AW_WORKSPACE_HOME is
# set explicitly instead: it's what paths.py's workspace_home() actually reads
# for F4 app state (bin shims, secrets, skills), and it must keep pointing at
# /opt/aw-workspace/.aw-workspace to match the hardcoded PATH entry below and
# the existing host bind-mount — decoupled from $HOME on purpose.
ENV AW_PORT=9030 \
    AW_WORKSPACE_WORKERS=5 \
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
