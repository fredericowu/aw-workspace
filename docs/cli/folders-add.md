---
source: aw-workspace-cli folders add
type: cli-reference
checksum: sha256:ba1d214d99669597b56de05c58d4e208ed9b5e6899306ecdb7ca04ebcfba741d
captured: 2026-10-11
edited: false
---

# aw-workspace-cli folders add --help

**Como eu uso o comando `aw-workspace-cli folders add`?** / **How do I use `aw-workspace-cli folders add`?**

Map a folder (re-mapping a name updates it)

`aw-workspace-cli folders add`

```
usage: aw-workspace-cli folders add [-h] [--name NAME] [--mode {ro,rw}] path

positional arguments:
  path            Absolute path to the directory to map

options:
  -h, --help      show this help message and exit
  --name NAME     Handle to address it by (default: the basename)
  --mode {ro,rw}  Mount mode handed to app containers (default: ro)
```
