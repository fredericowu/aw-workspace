---
source: aw-workspace-cli sideload
type: cli-reference
checksum: sha256:46b53dc95070db59e4f4e456b67c1c3dd980a129ff24e19c73fe121b93688525
captured: 2026-10-11
edited: false
---

# aw-workspace-cli sideload --help

**Como eu instalo um app a partir de uma pasta local, pra testar?** / **How do I install an app from a local directory, to test it?**

Install an app from a local directory (dev loop, no marketplace)

`aw-workspace-cli sideload`

```
usage: aw-workspace-cli sideload [-h] [--keep] path

Install an app from a local directory (dev loop, no marketplace)

positional arguments:
  path        Path to the app package dir (holds aw-app.json)

options:
  -h, --help  show this help message and exit
  --keep      Do not uninstall first — fail if the app is already installed

Redeploy after an edit: just run it again.
```
