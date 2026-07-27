# aw-workspace

Data plane de UM workspace — repositório novo, sem git history do
monólito. Skeleton (F2) do plano "Opção B rev.3/4" (aw-backend = control
plane central; aw-workspace = runtime por-workspace, um processo por
workspace, isolado por schema Postgres).

## Escopo deste repo (F2)

1. **FastAPI multi-worker** — `src/start/workspace.py`, `AW_WORKSPACE_WORKERS`.
2. **Isolamento por `schema_translate_map`** (`src/api/db.py`) — o coração
   do F2. O engine deste processo é criado com
   `execution_options(schema_translate_map={None: AW_WORKSPACE_SCHEMA})`;
   todo model é declarado SEM schema, então o processo só enxerga o schema
   do seu próprio workspace. O schema em si é provisionado centralmente por
   `workspace_provisioner.provision_schema()` no aw-backend (F1) —
   `CREATE SCHEMA workspace_<slug>` — este repo nunca cria schema, só tabelas
   dentro dele.
3. **Verificação offline do JWT EdDSA** (`src/api/identity.py`) —
   `require_identity` valida o token assinado pelo aw-backend
   (`Authorization: Bearer` ou cookie `aw_id_jwt`) usando só a CHAVE
   PÚBLICA (`AW_AUTH_PUBLIC_KEY` ou buscada uma vez de
   `AW_BACKEND_URL/api/identity/public-key`). Sem DB de credenciais aqui —
   identidade é central.
4. **Tabela baseline `settings`** (KV JSONB) — só para provar o round-trip
   isolado do translate map. Tabelas de runtime (apps/app_configs/runs) são
   escopo do F5.

Runtime completo (apps/presentation/gateway/redis) é **F5** — fora de escopo
aqui.

## Migração strangler-fig (rotas portadas do monólito)

O runtime cresce **portando rotas do monólito `agentic-workspace` uma a uma**,
mantendo o contrato de API/WS idêntico pra o SPA (`repos/aw-frontend`) não mudar.
Receita repetível + mapa "rota → plano" em **[`MIGRATION.md`](./MIGRATION.md)**.

- **#1 Terminal** — `/api/terminals*` (CRUD) + `/ws/terminal/*` (PTY) rodam
  **nesta máquina** (o shell é spawnado no host BYOD). Ver `src/api/terminal.py`
  e `src/api/terminal_manager.py`. Sessão em memória → processo single-worker
  (`AW_WORKSPACE_WORKERS=1`). `/api/v2/agent-sessions` existe mas vem vazio (a
  imagem slim ainda não traz CLIs de agente).

## Rodando local

```bash
cp .env.example .env
# editar AW_WORKSPACE / AW_WORKSPACE_SCHEMA / AWSERV_DB_URL
pip install -r requirements.txt
python -m src.start.workspace
curl localhost:9030/api/health
```

## Testes

```bash
pytest src/tests -v
```

`src/tests/test_isolation.py` prova o isolamento: dois engines
(`schema_translate_map` apontando para `workspace_a`/`workspace_b`)
inserindo em `settings` e cada um só enxergando as próprias linhas — mesmo
padrão SQLite/PG usado nos testes do aw-backend
(`src/tests/unit/api/test_workspace_provisioner.py`), roda contra o
Postgres real em `127.0.0.1:5432` e pula (skip) se não estiver acessível.

`src/tests/test_identity.py` assina um JWT EdDSA com uma chave de teste,
valida que `require_identity` aceita o token correto e rejeita um assinado
com outra chave / expirado / malformado.

## Envs

| Var | Descrição |
|---|---|
| `AW_WORKSPACE` | slug do workspace que este processo serve |
| `AW_WORKSPACE_SCHEMA` | schema Postgres já provisionado pelo aw-backend (`workspace_<slug>`) |
| `AW_PORT` | porta do uvicorn (default `9030`) |
| `AW_WORKSPACE_WORKERS` | nº de workers uvicorn (default `1`; >1 usa a factory string) |
| `AWSERV_DB_URL` | URL do Postgres compartilhado (managed) ou local/BYO |
| `AW_BACKEND_URL` | base URL do aw-backend, usada para buscar a chave pública se `AW_AUTH_PUBLIC_KEY` não estiver setada |
| `AW_AUTH_PUBLIC_KEY` | PEM da chave pública Ed25519 do aw-backend (opcional — evita o fetch de boot) |
