# Standard — choosing backing services for the aw-stack

**Status:** shipped. Opened 2026-11-07 from
`aw-autoskill-minio-image-discontinued-use-seaweedfs` for Kanban
`4115bf3b-9510-81fb-8509-de540f23bca0`, target
`aw-autoskill-registry-sync-triage`.
**Scope:** the durable *decisions* about what backs a service in the aw-stack
`docker-compose.yml` — which upstream image a given capability uses, and why.
The procedures live elsewhere:
[`../runbooks/adding-an-aw-stack-service.md`](../runbooks/adding-an-aw-stack-service.md)
for standing one up,
[`../runbooks/shipping-and-proving-a-deploy.md`](../runbooks/shipping-and-proving-a-deploy.md)
for deploying one.

This file exists to stop a settled choice from being re-litigated by the next
architect who defaults to the obvious upstream and spends tool calls
rediscovering why it does not work. **Each entry records one decision: the
obvious choice, why it is not available, what to use instead, and what is
already pointed at the old name.** Add an entry when you make such a choice;
do not create a new one-line standard for it.

---

## 1. Object storage (S3-compatible) — SeaweedFS, not MinIO

**Decided 2026-10-10.** The obvious choice for self-hosted S3-compatible
object storage is `minio/minio`. It is not installable: **every tag, current
and old, returns HTTP 410** — confirmed from Docker Hub *and* from MinIO's
own official CDN, with an unrelated image from the same registry pulling
successfully in the same session to rule out a network or registry problem.
MinIO has discontinued free binary and image distribution.

Use `chrislusf/seaweedfs` (tested at `4.48`) in S3 gateway mode:

```yaml
aw-s3:
  image: chrislusf/seaweedfs:4.48
  # S3 gateway mode — see the seaweedfs docs for the `s3` subcommand and flags
```

SeaweedFS speaks the S3 API and is a drop-in for `boto3` clients — nothing
downstream needs to know the backing service changed.

**Keep the planned service name.** If earlier planning docs or Kanban cards
already reference the service as `aw-minio`, add a compose/Caddy alias rather
than renaming everything that points at it. Swap the image, keep the name
stable.

**Do not re-litigate this.** A MinIO pull failure is a known, confirmed-dead
distribution channel — go straight to SeaweedFS instead of re-diagnosing
network or registry issues.

## 2. Images come pre-built; aw-stack does not build them

A property of the stack worth knowing before you choose anything: the
"Deploy aw-stack" workflow (`tekflox/aw-stack`, `workflow_dispatch -f
services=…`) recreates the named containers from **whatever image tag is
already on GHCR**. It never builds from source. A new service therefore needs
its own repo, its own build workflow and its own GHCR push before the stack
can run it at all — see
[`../runbooks/shipping-and-proving-a-deploy.md`](../runbooks/shipping-and-proving-a-deploy.md)
§1 for the ordering this implies.

---

## Provenance

- `aw-autoskill-minio-image-discontinued-use-seaweedfs` → §1

§2 is cross-referenced context, not a separate finding; its own source row
landed in the shipping runbook.
