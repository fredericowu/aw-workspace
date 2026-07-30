# aw-workspace

Per-workspace runtime service for Agentic Workspace. Each process serves one
workspace, uses a dedicated Postgres schema, validates identity tokens, and
hosts workspace-level API and WebSocket routes.

## Scope

- **FastAPI runtime**: `src/start/workspace.py`, controlled by
  `AW_WORKSPACE_WORKERS`.
- **Schema isolation**: `src/api/db.py` creates engines with
  `schema_translate_map={None: AW_WORKSPACE_SCHEMA}` so models only read and
  write the current workspace schema.
- **Identity validation**: `src/api/identity.py` validates Ed25519 JWTs from
  the backend using `AW_AUTH_PUBLIC_KEY` or
  `AW_BACKEND_URL/api/identity/public-key`.
- **Baseline settings table**: a JSONB key/value table used by tests and
  runtime configuration checks.
- **Terminal API**: `/api/terminals*` and `/ws/terminal/*` provide local PTY
  sessions for the workspace process.

## Local Run

```bash
cp .env.example .env
# edit AW_WORKSPACE, AW_WORKSPACE_SCHEMA, and AWSERV_DB_URL
pip install -r requirements.txt
python -m src.start.workspace
curl localhost:9030/api/health
```

## Tests

```bash
pytest src/tests -v
```

`src/tests/test_isolation.py` verifies that separate workspace schemas cannot
see each other's `settings` rows. The Postgres-backed test uses
`127.0.0.1:5432` when available and skips when it is not reachable.

`src/tests/test_identity.py` signs a JWT with a test key and verifies that
`require_identity` accepts the correct token while rejecting wrong,
expired, or malformed tokens.

## Environment

| Var | Description |
|---|---|
| `AW_WORKSPACE` | Workspace slug served by this process |
| `AW_WORKSPACE_SCHEMA` | Existing Postgres schema for this workspace |
| `AW_PORT` | Uvicorn port, default `9030` |
| `AW_WORKSPACE_WORKERS` | Uvicorn worker count, default `1` |
| `AWSERV_DB_URL` | Shared Postgres URL |
| `AW_BACKEND_URL` | Backend base URL used to fetch the public identity key |
| `AW_AUTH_PUBLIC_KEY` | Optional Ed25519 public key PEM |
