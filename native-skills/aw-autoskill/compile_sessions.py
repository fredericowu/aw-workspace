#!/usr/bin/env python3
"""
aw-autoskill/compile_sessions.py

Reads Claude Code session .jsonl files from ~/.claude/projects/-opt-aw-workspace/,
filters for sessions newer than last_run stored in aw-autoskill.json, and compiles
a structured summary for LLM analysis.

Ported from the agentic-workspace monolith's aw-autoskill skill. Telegram-message
mining was dropped in the port: in this workspace, every Telegram turn (via
aw-agent-telegram) already runs as its own Claude Code CLI session, so it shows
up in the .jsonl scan below — there is no separate side-channel to mine like the
monolith's awserv `telegram_messages` table was. If you need a specific user's
raw Telegram history for some other reason, use the agents-platform-runners MCP
tools `list_callers` (source="telegram") + `list_caller_messages` instead of
reaching for direct DB access — this script's container has no DB credentials.

Usage:
  python3 native-skills/aw-autoskill/compile_sessions.py [--all] [--max-sessions N]

Output: JSON to stdout with:
  - session messages (user/assistant text + tool call summaries)
  - bash_commands: top recurring Bash commands per session (for workflow pattern detection)
  - user_corrections: messages where the user corrected agent behavior (for skill opportunities)
"""

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

WORKSPACE_DIR = Path(os.environ.get("AW_WORKSPACE_CONTAINER_DIR", "/opt/aw-workspace"))
SESSIONS_DIR = Path.home() / ".claude" / "projects" / "-opt-aw-workspace"
STATE_FILE = WORKSPACE_DIR / ".aw-workspace" / "data" / "aw-autoskill" / "aw-autoskill.json"
MAX_MSG_LEN = 800    # truncate long individual messages
MAX_SESSIONS = 15    # cap to avoid overwhelming the LLM
MAX_BASH_PREVIEW = 100  # chars of bash command to keep for pattern detection

# Phrases that signal the user corrected or redirected the agent
CORRECTION_SIGNALS = [
    # English
    r"\bno[,\.]?\b", r"\bwrong\b", r"\bthat'?s not\b", r"\bnot that\b",
    r"\bdon'?t\b", r"\bstop\b", r"\bundo\b", r"\brevert\b", r"\bno,?\s+",
    r"\bthat'?s wrong\b", r"\bincorrect\b", r"\bnot what i\b",
    r"\byou missed\b", r"\byou forgot\b", r"\btry again\b", r"\bnot right\b",
    r"\bnão era isso\b", r"\nerrado\b",
    # Portuguese
    r"\bnão\b", r"\berrado\b", r"\berro\b", r"\bcorrige\b", r"\bcorrigi\b",
    r"\bnão era\b", r"\bnão é isso\b", r"\bnão tô\b", r"\bnão to\b",
    r"\bvolta\b", r"\bdesfaz\b", r"\brefaz\b", r"\brecomeça\b",
    r"\bprecisa corrigir\b", r"\bcorrigir\b", r"\bnão funciona\b",
    r"\bnão funcionou\b", r"\bnão tá\b", r"\bnão ta\b",
    r"\btá errado\b", r"\bta errado\b",
]
CORRECTION_RE = re.compile("|".join(CORRECTION_SIGNALS), re.IGNORECASE)

# Noise prefixes to skip in user messages
SKIP_PREFIXES = (
    "local-command", "<command", "PRESENTATION INSTRUCTIONS",
    "[Request interrupted", "[SYSTEM]", "This session is being continued",
)


def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"last_run": None, "history": []}


def _is_noise(content: str) -> bool:
    return any(s in content for s in SKIP_PREFIXES)


def extract_session_data(jsonl_path: Path) -> dict:
    """
    Extract from a session .jsonl:
    - messages: user/assistant turns (text + tool call names+key args)
    - bash_commands: Counter of top recurring bash command prefixes
    - user_corrections: list of user messages that appear to correct the agent
    """
    messages: list[dict] = []
    bash_cmds: list[str] = []
    user_corrections: list[str] = []

    try:
        with open(jsonl_path, encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                if event.get("type") not in ("user", "assistant"):
                    continue
                if event.get("isMeta"):
                    continue

                msg = event.get("message", {})
                role = msg.get("role")
                content = msg.get("content", "")

                # Content may be a list of content blocks
                if isinstance(content, list):
                    parts = []
                    for block in content:
                        if not isinstance(block, dict):
                            parts.append(str(block))
                            continue
                        btype = block.get("type")
                        if btype == "text":
                            parts.append(block.get("text", ""))
                        elif btype == "tool_use":
                            name = block.get("name", "?")
                            inp = block.get("input", {})
                            summary = f"[tool_use: {name}"
                            # Include key args for all tools
                            for k in ("command", "path", "file_path", "query", "prompt", "input"):
                                if k in inp:
                                    val = str(inp[k])
                                    # For bash commands: capture full prefix for pattern analysis
                                    if k == "command" and name == "Bash":
                                        bash_cmds.append(val[:MAX_BASH_PREVIEW])
                                    summary += f" {k}={val[:80]!r}"
                            summary += "]"
                            parts.append(summary)
                        # skip tool_result blocks — too noisy
                    content = " ".join(p for p in parts if p.strip())

                if not content or not role:
                    continue

                # Detect user corrections
                if role == "user" and not _is_noise(content):
                    if CORRECTION_RE.search(content):
                        correction_text = content[:300].replace("\n", " ")
                        user_corrections.append(correction_text)

                # Truncate long messages
                if len(content) > MAX_MSG_LEN:
                    content = content[:MAX_MSG_LEN] + " …[truncated]"

                # Skip noise user messages from the message list
                if role == "user" and _is_noise(content):
                    continue

                messages.append({
                    "role": role,
                    "content": content,
                    "timestamp": event.get("timestamp"),
                })

    except Exception as exc:
        sys.stderr.write(f"Warning: could not read {jsonl_path}: {exc}\n")

    # Top recurring bash command prefixes (prefix = first 60 chars, count > 1)
    bash_counter = Counter(cmd[:60] for cmd in bash_cmds)
    top_bash = [
        {"cmd_prefix": cmd, "count": cnt}
        for cmd, cnt in bash_counter.most_common(20)
        if cnt > 1
    ]

    return {
        "messages": messages,
        "bash_commands": top_bash,
        "user_corrections": user_corrections,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Compile Claude sessions for skill analysis.")
    parser.add_argument("--all", action="store_true", help="Ignore last_run; analyze all sessions.")
    parser.add_argument("--max-sessions", type=int, default=MAX_SESSIONS)
    args = parser.parse_args()

    state = load_state()
    last_run: str | None = None if args.all else state.get("last_run")

    if not SESSIONS_DIR.exists():
        sys.stderr.write(f"Warning: sessions dir does not exist: {SESSIONS_DIR}\n")
        all_files: list[Path] = []
    else:
        # Collect .jsonl files sorted newest-first
        all_files = sorted(
            SESSIONS_DIR.glob("*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

    # Filter to only files modified after last_run
    if last_run:
        cutoff = datetime.fromisoformat(last_run.replace("Z", "+00:00")).timestamp()
        all_files = [p for p in all_files if p.stat().st_mtime > cutoff]

    # Cap (--all bypasses the cap too for sessions)
    session_files = all_files if args.all else all_files[: args.max_sessions]

    sessions = []
    for sf in session_files:
        data = extract_session_data(sf)
        if not data["messages"] and not data["bash_commands"] and not data["user_corrections"]:
            continue
        sessions.append({
            "session_id": sf.stem,
            "modified_at": datetime.fromtimestamp(sf.stat().st_mtime, tz=timezone.utc).isoformat(),
            "message_count": len(data["messages"]),
            "bash_commands": data["bash_commands"],
            "user_corrections": data["user_corrections"],
            "messages": data["messages"],
        })

    result = {
        "last_run": last_run,
        "analyzed_since": last_run or "beginning",
        "session_count": len(sessions),
        "sessions_dir": str(SESSIONS_DIR),
        "sessions": sessions,
    }

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
