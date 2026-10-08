"""Generate the ``cli_reference/`` and ``skills/`` knowledge-base content.

Card ``quality:procedural-genre-absent-from-both-knowledge-indexes``: both
the ``kb`` pgvector store and the aw-knowledgeable graph measured ~83%
auto-generated code maps plus ~17% Notion cards — no procedural "how do I"
genre at all, so three ordinary operational questions scored 0/3 on both
sides even though the answers were two ``--help`` calls away. This module
produces that missing genre from two sources that already exist on disk and
cost no LLM tokens to capture:

* ``aw-workspace-cli <command> [<subcommand>] --help`` — one document per
  command, walking the SAME dynamic tree ``src/cli/discovery.py`` builds
  (built-in ``src/cli/commands/*.py`` plus every installed app's own
  ``commands/*.py``), so a new command shows up here with no change to this
  file.
* ``skills/<name>/SKILL.md`` — the materialized skill tree (AGENTS.md:
  ``skills/`` is generated, the sources are ``native-skills/<name>/`` and
  each app's ``contributes.skills``) is procedural prose written for exactly
  this purpose, and was previously indexed only via the separate
  ``search_skills`` surface — never as content an ordinary knowledge search
  would ever surface.

Both write straight into ``<AW_WORKSPACE_HOME>/knowledge_base/<prefix>/*.md``
— the exact tree ``kb_app.kb_ops._build`` walks for the pgvector store (any
``.md`` anywhere under ``KB_DIR``, see ``src/apps/runtime.py``'s
``$AW_KB_DIR`` mount comment), so a plain
``aw-workspace-cli knowledge-base --build`` after this picks them up with no
further change on that side. The aw-knowledgeable graph side additionally
needs its own bucket declared (``BUCKET_ORDER`` in aw-app-knowledgeable's
``bulk_ingest.py``), since that driver only scans a fixed allowlist of
top-level subtrees — that edit lives in that app's own repo, not here.

Wired to a scheduled task (see ``aw-workspace-cli kb-reference``) rather than
into ``agent_sync.sync_all()`` / the boot path: capturing ``--help`` for
every discovered command shells out once per command/subcommand, which is
cheap in isolation but would add a user-visible delay to every boot and to
every manual ``agent sync`` — a periodic tick (this content changes only
when a command's flags change) is the better fit than "redo it on every
server start."
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import subprocess
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from src.apps import paths
from src.cli.discovery import discover_commands
from src.libs import skills_sync

log = logging.getLogger(__name__)

CLI_BIN = "aw-workspace-cli"
_HELP_TIMEOUT = 30


def kb_dir() -> Path:
    """``<AW_WORKSPACE_HOME>/knowledge_base`` — the same tree ``kb_app.kb_ops``
    indexes and aw-knowledgeable's bulk-ingest driver reads (its own
    ``kb_root()``)."""
    return Path(paths.workspace_home()) / "knowledge_base"


@dataclass
class ReferenceSyncResult:
    written: int = 0
    failed: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"written": self.written, "failed": self.failed}


def _write_doc(rel_path: Path, source_label: str, doc_type: str, body: str, captured: str) -> None:
    checksum = hashlib.sha256(body.encode()).hexdigest()
    out_path = kb_dir() / rel_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = (
        "---\n"
        f"source: {source_label}\n"
        f"type: {doc_type}\n"
        f"checksum: sha256:{checksum}\n"
        f"captured: {captured}\n"
        "edited: false\n"
        "---\n\n"
    )
    out_path.write_text(frontmatter + body, encoding="utf-8")


# --- CLI --help -----------------------------------------------------------


def _subcommands(module) -> list[str]:
    """Names of a command's own subcommands, if it declares any via
    argparse subparsers. Only modules exposing a module-level
    ``_build_parser()`` are introspectable this way (``agent``,
    ``marketplace``, ``folders``, ...); a command whose argparse logic
    lives in an imported app package (e.g. ``secrets``) is walked as a
    single top-level entry only — still correct, just not recursed into."""
    build_parser = getattr(module, "_build_parser", None)
    if build_parser is None:
        return []
    try:
        parser = build_parser()
    except Exception:  # noqa: BLE001 — one odd command must not break the walk
        return []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return sorted(action.choices)
    return []


def _capture_help(command_path: list[str]) -> str | None:
    try:
        proc = subprocess.run(
            [CLI_BIN, *command_path, "--help"],
            capture_output=True, text=True, timeout=_HELP_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("cli_reference: %s --help failed: %s", " ".join(command_path), exc)
        return None
    return proc.stdout or proc.stderr


def sync_cli_reference() -> ReferenceSyncResult:
    """Capture ``--help`` for the root command, every discovered command,
    and every subcommand it declares, and write one document per capture
    under ``cli_reference/``."""
    result = ReferenceSyncResult()
    captured = date.today().isoformat()

    command_paths: list[list[str]] = [[]]
    for name, module in sorted(discover_commands().items()):
        command_paths.append([name])
        for sub in _subcommands(module):
            command_paths.append([name, sub])

    for command_path in command_paths:
        label = (CLI_BIN + " " + " ".join(command_path)).strip()
        text = _capture_help(command_path)
        if text is None:
            result.failed.append(label)
            continue
        slug = "-".join(command_path) or "root"
        body = f"# {label} --help\n\n`{label}`\n\n```\n{text.rstrip()}\n```\n"
        _write_doc(Path("cli_reference") / f"{slug}.md", label, "cli-reference", body, captured)
        result.written += 1

    return result


# --- skills/*/SKILL.md as content ------------------------------------------


def sync_skills_reference() -> ReferenceSyncResult:
    """Index every materialized ``skills/<name>/SKILL.md`` as content under
    ``skills/``, recording which app owns it (``.aw-app-id``) or that it's
    native (``native-skills/``) per AGENTS.md's ownership rule."""
    result = ReferenceSyncResult()
    captured = date.today().isoformat()

    src_dir = skills_sync.source_dir()
    if not src_dir.is_dir():
        return result

    for skill_dir in sorted(p for p in src_dir.iterdir() if p.is_dir()):
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            continue
        label = f"skills/{skill_dir.name}/SKILL.md"
        try:
            content = skill_md.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("cli_reference: could not read %s: %s", skill_md, exc)
            result.failed.append(label)
            continue

        owner_marker = skill_dir / ".aw-app-id"
        if owner_marker.is_file():
            owner = f"app: {owner_marker.read_text(encoding='utf-8').strip()}"
        else:
            owner = "native-skills/ (this repo)"

        body = f"# skill: {skill_dir.name}\n\nOwner: {owner}\n\n{content}\n"
        _write_doc(Path("skills") / f"{skill_dir.name}.md", label, "skill-content", body, captured)
        result.written += 1

    return result
