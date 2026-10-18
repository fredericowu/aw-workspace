#!/usr/bin/env python3
"""Load the Agents Platform execution history into ``exec-retrieval-neo4j``.

Context — card ``3fd5bf3b-9510-812c-b6bd-cf12aaff0194`` (retrieval test v2),
plan ``docs/design/execution-history-retrieval-test-plan.md``. v1 of this
harness loaded a **metadata-only** graph and scored 5/10; three of its losses
were caused by the absence of run text and nothing else. This script rebuilds
the v1 vocabulary unchanged and adds the text layer on top.

What it builds (full rebuild — it WIPES the target graph first, so re-running
it is the supported way to refresh):

    metadata (identical to v1, same counts)
      (:Run {id, agent_slug, status, model_slug, flow_slug, flow_outcome,
             hop_count, cost_usd, tokens_in, tokens_out, started_at, ended_at})
      (:Run)-[:CHILD_OF]->(:Run)
      (:Run)-[:QA_OF]->(:Run)
      (:Run)-[:IN_SESSION]->(:Session {id})
      (:Run)-[:ON_CARD]->(:Card {page_id})
      (:Run)-[:CALLED {n}]->(:Tool {name})

    text layer (new in v2)
      Run.input_text, Run.output_text          -- full, untruncated
      (:Run)-[:MADE_CALL]->(:ToolCall {seq, name, args_text, ts})
      full-text index run_text      over Run(input_text, output_text)
      full-text index toolcall_text over ToolCall(name, args_text)

    The CALLED rollup is kept ALONGSIDE MADE_CALL, not replaced by it.

Two schema facts that cost v1 real time — do not re-derive them:

* ``runs.agent_slug`` **does not exist** on the live
  ``agents_platform_multitenant`` schema. The column carrying the agent
  identity (``coder-sonnet``, ``architect``, ``telegram-opus-aw``, …) is
  ``runs.source_slug``. The graph property is still named ``agent_slug``
  because that is the name the arm prompts and the v1 Cypher use.
* ``run_events.payload`` is a ``json`` column and some payloads contain a
  literal ``\\u0000``. Any server-side ``payload::text`` or ``payload->>`` over
  the *whole* document therefore fails with "unsupported Unicode escape
  sequence". This script never casts the document server-side: psycopg2 parses
  it client-side (where ``\\u0000`` is legal) and ``_clean()`` strips the NULs
  before they reach Neo4j.

SECURITY. The loaded text carries live credentials pasted into agent runs, so
``exec-retrieval-neo4j`` is a secret-bearing store: it runs with **no published
port**, reachable only over the nested podman network inside ``aw-remote-host``.
Its password lives in the workspace secret store (namespace ``_exec_retrieval``,
key ``exec_retrieval_neo4j_password``) and is read from there at runtime — it is
never an argument, never a literal, and is scrubbed from any error this script
prints. The Postgres password is NOT copied into the secret store; pass it in
per-run (``--pg-password-file``, or ``PGPASSWORD``), e.g. from
``docker exec aw-stack-aw-postgres-1 printenv POSTGRES_PASSWORD``.

Usage (from any container on the nested ``aw-remote-host`` podman network that
can also reach the aw-stack Postgres — an agent sandbox can do both):

    python3 scripts/load_exec_retrieval_neo4j.py --pg-password-file /path/to/pw
    python3 scripts/load_exec_retrieval_neo4j.py --verify-only
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from base64 import b64encode

# The test's frozen window — tenant + cutoff. Every count in the card's
# verification table is defined against exactly this scope.
TENANT = "tenant-bootstrap"
T0 = "2026-10-18 11:43:38"

CONTAINER = "exec-retrieval-neo4j"
SECRETS_NS = "_exec_retrieval"
SECRETS_KEY = "exec_retrieval_neo4j_password"

# aw-stack-aw-postgres-1 on aw-stack-net. Reachable by IP (not by name) from
# inside the nested podman, which NATs out through aw-remote-host's netns.
PG_HOST = os.environ.get("AW_EXEC_PG_HOST", "172.22.0.3")
PG_PORT = int(os.environ.get("AW_EXEC_PG_PORT", "5432"))
PG_DB = os.environ.get("AW_EXEC_PG_DB", "agents_platform_multitenant")
PG_USER = os.environ.get("AW_EXEC_PG_USER", "postgres")

# Bytes of serialised row payload per HTTP transaction. Keeps a single commit
# well inside the container's 2G heap while still amortising round-trips.
BATCH_BYTES = 4_000_000


def _clean(value):
    """Strip NUL from anything heading for Neo4j (see module docstring)."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    return value


def _as_text(value):
    """A json column -> one string. dicts/lists are re-serialised compactly."""
    if value is None:
        return None
    if isinstance(value, str):
        return _clean(value)
    return _clean(json.dumps(value, ensure_ascii=False))


# --------------------------------------------------------------------------
# connections
# --------------------------------------------------------------------------

def neo4j_password() -> str:
    """Read the password out of the workspace secret store.

    Imported by path rather than installed: this script runs from agent
    sandboxes where ``/opt/aw-workspace`` is mounted but not importable. Note
    that *setting* ``PYTHONPATH`` instead breaks the workspace venv's own
    site-packages resolution in those containers, which is why this inserts
    into ``sys.path`` directly.
    """
    root = os.environ.get("AW_WORKSPACE_CONTAINER_DIR", "/opt/aw-workspace")
    if root not in sys.path:
        sys.path.insert(0, root)
    from src.apps.secret_store import SecretStore  # noqa: PLC0415

    pw = SecretStore().get(SECRETS_NS, SECRETS_KEY)
    if not pw:
        raise SystemExit(
            f"no {SECRETS_KEY} in the secret store namespace {SECRETS_NS} — "
            "the container's password is unrecoverable from here; recreate it"
        )
    return pw


def neo4j_base_url() -> str:
    """Resolve the HTTP endpoint, preferring a live container inspect.

    The bridge IP is NOT stable across a recreate (v1 sat on 10.89.0.111; this
    one came up on another address), so the address is re-read every run rather
    than written down. ``AW_EXEC_RETRIEVAL_NEO4J_URL`` overrides for callers
    with no container-engine socket.
    """
    override = os.environ.get("AW_EXEC_RETRIEVAL_NEO4J_URL")
    if override:
        return override.rstrip("/")
    out = subprocess.run(
        ["docker", "inspect", CONTAINER, "--format",
         "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if not out:
        raise SystemExit(f"{CONTAINER} has no bridge IP — is it running?")
    return f"http://{out}:7474"


class Neo4j:
    """Minimal client over the HTTP transaction endpoint.

    Deliberately not the ``neo4j`` Python driver: this has to run in agent
    sandboxes that have no pip install, and the HTTP route is the one proven
    reachable from a plain ``aw-warm-*`` container (which has neither a driver
    nor a container socket to ``docker exec`` through).
    """

    def __init__(self, base_url: str, password: str, user: str = "neo4j"):
        self.url = f"{base_url}/db/neo4j/tx/commit"
        self._password = password
        token = b64encode(f"{user}:{password}".encode()).decode()
        self._headers = {
            "Content-Type": "application/json",
            "Authorization": f"Basic {token}",
        }

    def _scrub(self, text: str) -> str:
        return text.replace(self._password, "<REDACTED>")

    def run(self, statement: str, parameters: dict | None = None, retries: int = 3):
        body = {"statements": [{"statement": statement,
                                "parameters": parameters or {}}]}
        payload = json.dumps(body).encode()
        last = None
        for attempt in range(retries):
            req = urllib.request.Request(self.url, data=payload,
                                         headers=self._headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=600) as resp:
                    out = json.loads(resp.read())
                if out.get("errors"):
                    raise RuntimeError(self._scrub(json.dumps(out["errors"])))
                return out["results"][0]
            except urllib.error.HTTPError as exc:  # auth / 5xx
                last = RuntimeError(self._scrub(f"HTTP {exc.code}: {exc.read()[:400]!r}"))
            except (urllib.error.URLError, TimeoutError) as exc:
                last = RuntimeError(self._scrub(f"transport: {exc}"))
            time.sleep(2 * (attempt + 1))
        raise last

    def scalar(self, statement: str, parameters: dict | None = None):
        res = self.run(statement, parameters)
        data = res["data"]
        return data[0]["row"][0] if data else None

    def wait_ready(self, timeout_s: int = 180) -> None:
        deadline = time.time() + timeout_s
        while True:
            try:
                self.scalar("RETURN 1")
                return
            except Exception:
                if time.time() > deadline:
                    raise
                time.sleep(3)


def pg_connect(password: str):
    import psycopg2  # noqa: PLC0415

    return psycopg2.connect(host=PG_HOST, port=PG_PORT, user=PG_USER,
                            password=password, dbname=PG_DB, connect_timeout=15)


# --------------------------------------------------------------------------
# SQL — one place, so the graph and the verification table cannot drift
# --------------------------------------------------------------------------

RUN_SCOPE = f"r.tenant_id = %(tenant)s AND r.started_at < %(t0)s"

SQL = {
    "runs": f"""
        SELECT r.id, r.source_slug, r.status, r.model_slug, r.flow_slug,
               r.flow_outcome, r.hop_count, r.cost_usd, r.tokens_in,
               r.tokens_out, r.started_at, r.ended_at,
               r.parent_run_id, r.qa_run_id, r.session_id, r.notion_task_id,
               r.input, r.output
        FROM runs r WHERE {RUN_SCOPE}
    """,
    "tool_calls": f"""
        SELECT e.run_id, e.payload, e.ts,
               row_number() OVER (PARTITION BY e.run_id ORDER BY e.ts, e.id) AS seq
        FROM run_events e JOIN runs r ON r.id = e.run_id
        WHERE e.kind = 'tool_call' AND {RUN_SCOPE}
    """,
}

# The verification table. Each entry is (label, SQL, Cypher) and both sides are
# counted after the load; a mismatch is a hard failure, not a warning.
CHECKS = [
    ("Run",
     f"SELECT count(*) FROM runs r WHERE {RUN_SCOPE}",
     "MATCH (n:Run) RETURN count(n)"),
    ("CHILD_OF",
     f"""SELECT count(*) FROM runs r JOIN runs p ON p.id = r.parent_run_id
         AND p.tenant_id = %(tenant)s AND p.started_at < %(t0)s
         WHERE {RUN_SCOPE} AND r.parent_run_id IS NOT NULL""",
     "MATCH ()-[e:CHILD_OF]->() RETURN count(e)"),
    ("QA_OF",
     f"SELECT count(*) FROM runs r WHERE {RUN_SCOPE} AND r.qa_run_id IS NOT NULL",
     "MATCH ()-[e:QA_OF]->() RETURN count(e)"),
    ("ON_CARD",
     f"SELECT count(*) FROM runs r WHERE {RUN_SCOPE} AND r.notion_task_id IS NOT NULL",
     "MATCH ()-[e:ON_CARD]->() RETURN count(e)"),
    ("Card",
     f"""SELECT count(DISTINCT r.notion_task_id) FROM runs r
         WHERE {RUN_SCOPE} AND r.notion_task_id IS NOT NULL""",
     "MATCH (n:Card) RETURN count(n)"),
    ("IN_SESSION",
     f"SELECT count(*) FROM runs r WHERE {RUN_SCOPE} AND r.session_id IS NOT NULL",
     "MATCH ()-[e:IN_SESSION]->() RETURN count(e)"),
    ("Session",
     f"""SELECT count(DISTINCT r.session_id) FROM runs r
         WHERE {RUN_SCOPE} AND r.session_id IS NOT NULL""",
     "MATCH (n:Session) RETURN count(n)"),
    ("CALLED",
     f"""SELECT count(*) FROM (
             SELECT DISTINCT e.run_id, e.payload->>'name' AS n
             FROM run_events e JOIN runs r ON r.id = e.run_id
             WHERE e.kind = 'tool_call' AND {RUN_SCOPE}) x""",
     "MATCH ()-[e:CALLED]->() RETURN count(e)"),
    ("Tool",
     f"""SELECT count(DISTINCT e.payload->>'name')
         FROM run_events e JOIN runs r ON r.id = e.run_id
         WHERE e.kind = 'tool_call' AND {RUN_SCOPE}""",
     "MATCH (n:Tool) RETURN count(n)"),
    ("ToolCall",
     f"""SELECT count(*) FROM run_events e JOIN runs r ON r.id = e.run_id
         WHERE e.kind = 'tool_call' AND {RUN_SCOPE}""",
     "MATCH (n:ToolCall) RETURN count(n)"),
    ("MADE_CALL",
     f"""SELECT count(*) FROM run_events e JOIN runs r ON r.id = e.run_id
         WHERE e.kind = 'tool_call' AND {RUN_SCOPE}""",
     "MATCH ()-[e:MADE_CALL]->() RETURN count(e)"),
]

SCHEMA = [
    "CREATE CONSTRAINT run_id IF NOT EXISTS FOR (n:Run) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT session_id IF NOT EXISTS FOR (n:Session) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT card_page_id IF NOT EXISTS FOR (n:Card) REQUIRE n.page_id IS UNIQUE",
    "CREATE CONSTRAINT tool_name IF NOT EXISTS FOR (n:Tool) REQUIRE n.name IS UNIQUE",
]

FULLTEXT = [
    ("run_text",
     "CREATE FULLTEXT INDEX run_text IF NOT EXISTS "
     "FOR (n:Run) ON EACH [n.input_text, n.output_text]"),
    ("toolcall_text",
     "CREATE FULLTEXT INDEX toolcall_text IF NOT EXISTS "
     "FOR (n:ToolCall) ON EACH [n.name, n.args_text]"),
]


# --------------------------------------------------------------------------
# load phases
# --------------------------------------------------------------------------

def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def wipe(neo: Neo4j) -> None:
    """Delete everything in batches — a single DETACH DELETE over 300k+ nodes
    does not fit the heap."""
    while True:
        res = neo.run(
            "MATCH (n) WITH n LIMIT 20000 DETACH DELETE n RETURN count(n)")
        n = res["data"][0]["row"][0]
        if not n:
            break
        log(f"  wiped {n}")


def flush(neo: Neo4j, statement: str, rows: list) -> None:
    if rows:
        neo.run(statement, {"rows": rows})


def batched(neo: Neo4j, statement: str, rows_iter, label: str):
    """Send rows in BATCH_BYTES-sized transactions."""
    buf, size, total = [], 0, 0
    for row in rows_iter:
        buf.append(row)
        size += len(json.dumps(row, ensure_ascii=False))
        if size >= BATCH_BYTES:
            flush(neo, statement, buf)
            total += len(buf)
            log(f"  {label}: {total}")
            buf, size = [], 0
    flush(neo, statement, buf)
    total += len(buf)
    log(f"  {label}: {total} (done)")
    return total


RUN_CYPHER = """
UNWIND $rows AS row
CREATE (r:Run {
  id: row.id, agent_slug: row.agent_slug, status: row.status,
  model_slug: row.model_slug, flow_slug: row.flow_slug,
  flow_outcome: row.flow_outcome, hop_count: row.hop_count,
  cost_usd: row.cost_usd, tokens_in: row.tokens_in, tokens_out: row.tokens_out,
  started_at: row.started_at, ended_at: row.ended_at,
  input_text: row.input_text, output_text: row.output_text })
"""

EDGE_CYPHER = """
UNWIND $rows AS row
MATCH (r:Run {id: row.id})
FOREACH (_ IN CASE WHEN row.parent IS NULL THEN [] ELSE [1] END |
  MERGE (p:Run {id: row.parent}) MERGE (r)-[:CHILD_OF]->(p))
FOREACH (_ IN CASE WHEN row.qa IS NULL THEN [] ELSE [1] END |
  MERGE (q:Run {id: row.qa}) MERGE (r)-[:QA_OF]->(q))
FOREACH (_ IN CASE WHEN row.session IS NULL THEN [] ELSE [1] END |
  MERGE (s:Session {id: row.session}) MERGE (r)-[:IN_SESSION]->(s))
FOREACH (_ IN CASE WHEN row.card IS NULL THEN [] ELSE [1] END |
  MERGE (c:Card {page_id: row.card}) MERGE (r)-[:ON_CARD]->(c))
"""

CALLED_CYPHER = """
UNWIND $rows AS row
MATCH (r:Run {id: row.id})
MERGE (t:Tool {name: row.name})
MERGE (r)-[c:CALLED]->(t) SET c.n = row.n
"""

TOOLCALL_CYPHER = """
UNWIND $rows AS row
MATCH (r:Run {id: row.id})
CREATE (r)-[:MADE_CALL]->(:ToolCall {
  seq: row.seq, name: row.name, args_text: row.args_text, ts: row.ts })
"""


def load(neo: Neo4j, conn) -> None:
    params = {"tenant": TENANT, "t0": T0}

    log("wiping graph")
    wipe(neo)
    for stmt in SCHEMA:
        neo.run(stmt)

    # Pass 1 — Run nodes with their text. Also accumulates the edge endpoints
    # so the second pass doesn't need a second trip to Postgres. Edges are a
    # separate pass because CHILD_OF/QA_OF point at Run nodes that may not
    # exist yet while pass 1 is still streaming.
    log("loading Run nodes (+ input_text/output_text)")
    edges: list[dict] = []
    cur = conn.cursor("runs_cur")
    cur.itersize = 500
    cur.execute(SQL["runs"], params)

    def run_rows():
        for (rid, slug, status, model, fslug, foutcome, hop, cost, tin, tout,
             started, ended, parent, qa, session, card, inp, out) in cur:
            edges.append({"id": rid, "parent": parent, "qa": qa,
                          "session": session, "card": card})
            yield {
                "id": rid, "agent_slug": slug, "status": status,
                "model_slug": model, "flow_slug": fslug,
                "flow_outcome": foutcome, "hop_count": hop, "cost_usd": cost,
                "tokens_in": tin, "tokens_out": tout,
                "started_at": started.isoformat() if started else None,
                "ended_at": ended.isoformat() if ended else None,
                "input_text": _as_text(inp), "output_text": _as_text(out),
            }

    batched(neo, RUN_CYPHER, run_rows(), "runs")
    cur.close()

    # Pass 2 — the metadata edges. parent/qa endpoints outside the frozen
    # window are intentionally NOT merged into existence: filtering them here
    # is what makes CHILD_OF match the SQL join rather than the raw column.
    log("loading CHILD_OF / QA_OF / IN_SESSION / ON_CARD")
    known = {e["id"] for e in edges}
    for e in edges:
        if e["parent"] not in known:
            e["parent"] = None
        if e["qa"] not in known:
            e["qa"] = None
    batched(neo, EDGE_CYPHER, iter(edges), "edges")

    # Pass 3 — the CALLED {n} rollup, kept from v1 alongside MADE_CALL.
    log("loading CALLED {n} rollup")
    cur = conn.cursor("called_cur")
    cur.itersize = 2000
    cur.execute(f"""
        SELECT e.run_id, e.payload->>'name' AS name, count(*) AS n
        FROM run_events e JOIN runs r ON r.id = e.run_id
        WHERE e.kind = 'tool_call' AND {RUN_SCOPE}
        GROUP BY e.run_id, e.payload->>'name'
    """, params)
    batched(neo, CALLED_CYPHER,
            ({"id": rid, "name": _clean(name), "n": n} for rid, name, n in cur),
            "called")
    cur.close()

    # Pass 4 — one ToolCall node per tool_call event, with its arguments.
    log("loading ToolCall nodes (+ args_text)")
    cur = conn.cursor("tc_cur")
    cur.itersize = 1000
    cur.execute(SQL["tool_calls"], params)

    def tc_rows():
        for rid, payload, ts, seq in cur:
            payload = payload if isinstance(payload, dict) else {}
            yield {
                "id": rid, "seq": int(seq),
                "name": _clean(payload.get("name")),
                "args_text": _as_text(payload.get("input")),
                "ts": ts.isoformat() if ts else None,
            }

    batched(neo, TOOLCALL_CYPHER, tc_rows(), "toolcalls")
    cur.close()

    log("creating full-text indexes")
    for name, stmt in FULLTEXT:
        neo.run(stmt)
    for name, _ in FULLTEXT:
        deadline = time.time() + 900
        while True:
            state = neo.scalar(
                "SHOW INDEXES YIELD name, state WHERE name = $n RETURN state",
                {"n": name})
            if state == "ONLINE":
                log(f"  {name}: ONLINE")
                break
            if time.time() > deadline:
                raise SystemExit(f"index {name} stuck in state {state}")
            time.sleep(5)


def verify(neo: Neo4j, conn) -> bool:
    params = {"tenant": TENANT, "t0": T0}
    ok = True
    rows = []
    for label, sql, cypher in CHECKS:
        cur = conn.cursor()
        cur.execute(sql, params)
        sql_n = cur.fetchone()[0]
        cur.close()
        graph_n = neo.scalar(cypher)
        match = sql_n == graph_n
        ok = ok and match
        rows.append((label, sql_n, graph_n, match))
    width = max(len(r[0]) for r in rows)
    print()
    print(f"{'entity'.ljust(width)}  {'SQL':>10}  {'Neo4j':>10}  match")
    for label, sql_n, graph_n, match in rows:
        print(f"{label.ljust(width)}  {sql_n:>10}  {graph_n:>10}  "
              f"{'OK' if match else 'MISMATCH'}")
    print()
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pg-password-file",
                    help="file holding the aw-stack Postgres password "
                         "(defaults to $PGPASSWORD)")
    ap.add_argument("--verify-only", action="store_true",
                    help="re-run the count table against an already-loaded graph")
    args = ap.parse_args()

    if args.pg_password_file:
        with open(args.pg_password_file) as f:
            pg_pw = f.read().strip()
    else:
        pg_pw = os.environ.get("PGPASSWORD", "")
    if not pg_pw:
        raise SystemExit("no Postgres password: pass --pg-password-file or set PGPASSWORD")

    neo = Neo4j(neo4j_base_url(), neo4j_password())
    neo.wait_ready()
    conn = pg_connect(pg_pw)
    try:
        if not args.verify_only:
            t = time.time()
            load(neo, conn)
            log(f"load finished in {time.time() - t:.0f}s")
        ok = verify(neo, conn)
    finally:
        conn.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
