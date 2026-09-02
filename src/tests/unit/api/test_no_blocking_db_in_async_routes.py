"""Regression guard for the 2026-09-02 single-worker event-loop freeze.

Symptom: four unrelated GET routes (``/api/apps/status``, contributions,
``/api/settings/workspace-api-key``, ``/api/settings/mcp``) all returned 200
but took 23-25s each, right after a restart. Not a slow query — a frozen
event loop.

Root cause: ``src.api.db.get_session`` is a SYNCHRONOUS sqlmodel session over
the sync psycopg driver, and this process runs ONE uvicorn worker
(``AW_WORKSPACE_WORKERS=1``, deliberate — terminal PTY sessions keep
in-memory state). One worker means one asyncio event-loop thread serving
every concurrent request as a coroutine, so a ``get_session()`` call made
directly inside an ``async def`` blocks EVERY in-flight request for the whole
DB round-trip, including requests touching no database at all. The trigger
was ``_boot_reconcile_and_sync``: the reconcile pass walks every installed
app serially (47 in the live workspace), each step doing blocking mirror
reads/writes on that one thread.

This is the DB sibling of ``test_no_blocking_io_in_async_routes.py`` in
agentic-workspace / aw-backend, which guards the same class of bug for
blocking ``requests`` calls after the 2026-07-25 awserv outages.

If this test fails, the fix is one of:
  - ``await asyncio.to_thread(<the sync function>)`` at the call site — the
    default, and what ``src/apps/reconciler.py`` already does for its
    blocking GitHub fetches
  - make the handler a plain ``def`` instead of ``async def``, letting
    FastAPI run it in its own threadpool (see ``src/api/guest_users.py``)

WHAT THIS DELIBERATELY DOES NOT CATCH
-------------------------------------
Only two things are checked, both resolvable with no ambiguity:

  1. an ``async def`` calling ``get_session()`` directly;
  2. an ``async def`` calling a module-level function *defined in the same
     file* that itself calls ``get_session()`` directly.

It does NOT chase blocking calls through attribute access on another module's
object (``self.manager.get_pending()``, ``reconciler.local.upsert()``).
Resolving those by method name alone was tried and is worthless here: names
like ``list``, ``add``, ``delete`` and ``record`` collide with dozens of
unrelated methods, so the check produced far more false alarms than findings
and would have been disabled within a week. Those call sites are covered by
``src/tests/integration/api/test_event_loop_not_blocked_by_db.py`` (behaviour)
and by the rule documented on ``src.api.db.get_session`` (intent) instead.
"""
from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[3]

# Entry point that opens a blocking session. Unambiguous: nothing else in the
# tree defines or imports a different `get_session`.
SESSION_FN = "get_session"


def _iter_python_files(root: pathlib.Path):
    for path in sorted(root.rglob("*.py")):
        if "tests" in path.parts or "__pycache__" in path.parts:
            continue
        yield path


def _called_names(node: ast.AST) -> set[str]:
    """Every simple/attribute call name appearing under ``node``, NOT
    descending into nested function definitions (they have their own
    execution context — a sync closure called via to_thread is fine)."""
    names: set[str] = set()

    def walk(n: ast.AST) -> None:
        for child in ast.iter_child_nodes(n):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if isinstance(child, ast.Call):
                func = child.func
                if isinstance(func, ast.Name):
                    names.add(func.id)
                elif isinstance(func, ast.Attribute):
                    names.add(func.attr)
            walk(child)

    walk(node)
    return names


def _threaded_names(node: ast.AST) -> set[str]:
    """Names handed to ``asyncio.to_thread(fn, ...)`` — i.e. correctly moved
    off the event loop, so they must not be reported."""
    safe: set[str] = set()
    for sub in ast.walk(node):
        if (isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr == "to_thread"
                and sub.args):
            first = sub.args[0]
            if isinstance(first, ast.Name):
                safe.add(first.id)
            elif isinstance(first, ast.Attribute):
                safe.add(first.attr)
    return safe


def _violations(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))

    # Module-level sync functions in THIS file that open a session directly.
    local_blocking = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and SESSION_FN in _called_names(node)
    }
    flagged = {SESSION_FN} | local_blocking

    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        safe = _threaded_names(node)
        for name in sorted(_called_names(node) & flagged):
            if name in safe:
                continue
            out.append(f"{path}:{node.lineno} async def {node.name}() calls {name}()")
    return out


def test_no_blocking_db_session_inside_async_def():
    violations: list[str] = []
    for path in _iter_python_files(SRC / "src"):
        violations.extend(_violations(path))

    assert not violations, (
        "Blocking DB work on the single event-loop thread — this freezes EVERY "
        "concurrent request, not just this one (AW_WORKSPACE_WORKERS=1). Wrap "
        "the call in `await asyncio.to_thread(...)`, or make the handler a "
        "plain `def` so FastAPI threadpools it. See src/api/db.py's "
        "get_session docstring.\n  " + "\n  ".join(violations)
    )
