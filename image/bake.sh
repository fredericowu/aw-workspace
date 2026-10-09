#!/usr/bin/env bash
# Pré-provisiona na IMAGEM o que as apps do perfil instalariam em runtime.
#
# Uso: bake.sh <perfil>    (chamado pelo Dockerfile, como root, antes do USER)
#
# Lê image/baked.json — ver os comentários lá para a regra que decide o que
# pode e o que não pode ser assado. Este script é só o executor; a política
# vive no JSON, que é o ponto: trocar o conteúdo da imagem (ou criar um
# perfil novo para um template de workspace) é editar dados, não shell.
#
# Por que um script e não RUN inline no Dockerfile: um perfil é uma LISTA de
# apps, e cada app traz pacotes, repositórios de terceiros e instaladores
# que não são apt. Expressar isso em Dockerfile daria uma escada de RUN
# condicionais impossível de ler, e tornaria "gerar a imagem X com as apps
# A, B, C" uma edição de Dockerfile em vez de um build-arg.
set -euo pipefail

PROFILE="${1:?uso: bake.sh <perfil>}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST="$HERE/baked.json"

# python3 e não jq: a base é python:3.12-slim, então python3 está garantido
# e jq seria um pacote a mais na imagem só para ler um arquivo de build.
read_manifest() { python3 -c "
import json, sys
m = json.load(open('$MANIFEST'))
print(json.dumps(m$1))
"; }

apps=$(python3 -c "
import json
m = json.load(open('$MANIFEST'))
p = m['profiles'].get('$PROFILE')
if p is None:
    raise SystemExit('perfil desconhecido: $PROFILE — conhecidos: ' + ', '.join(m['profiles']))
missing = [a for a in p if a not in m['apps']]
if missing:
    raise SystemExit('perfil $PROFILE cita apps sem entrada em \"apps\": ' + ', '.join(missing))
print(' '.join(p))
")

echo "bake: perfil '$PROFILE' -> $apps"

export DEBIAN_FRONTEND=noninteractive

# ---- repositórios de terceiros, uma vez cada, antes de qualquer install ----
repos=$(python3 -c "
import json
m = json.load(open('$MANIFEST'))
seen = []
for a in '$apps'.split():
    r = m['apps'][a].get('apt_repo')
    if r and r not in seen:
        seen.append(r)
print(' '.join(seen))
")

if [ -n "${repos// /}" ]; then
  apt-get update -qq
  apt-get install -y --no-install-recommends ca-certificates curl gpg apt-transport-https
  for r in $repos; do
    eval "$(python3 -c "
import json, shlex
m = json.load(open('$MANIFEST'))['apt_repos']['$r']
for k in ('key_url','keyring','list','repo'):
    print(f'{k.upper()}={shlex.quote(m[k])}')
print('ARMORED=' + ('1' if m['key_is_armored'] else '0'))
")"
    echo "bake: repo apt '$r'"
    install -d -m 0755 "$(dirname "$KEYRING")"
    # A chave do GitHub já vem binária; a do Google vem ASCII-armored e
    # precisa de --dearmor. Errar isso dá "NO_PUBKEY" só no apt-get update
    # seguinte, que é longe o bastante para confundir.
    if [ "$ARMORED" = "1" ]; then
      curl -fsSL "$KEY_URL" | gpg --dearmor | tee "$KEYRING" >/dev/null
    else
      curl -fsSL "$KEY_URL" -o "$KEYRING"
    fi
    chmod go+r "$KEYRING"
    echo "deb [arch=$(dpkg --print-architecture) signed-by=$KEYRING] $REPO" > "$LIST"
  done
fi

# ---- pacotes apt, num único install para resolver dependências de uma vez --
packages=$(python3 -c "
import json
m = json.load(open('$MANIFEST'))
out = []
for a in '$apps'.split():
    out += m['apps'][a].get('apt', [])
print(' '.join(out))
")

if [ -n "${packages// /}" ]; then
  echo "bake: apt -> $packages"
  apt-get update -qq
  # shellcheck disable=SC2086 — lista de pacotes, split é intencional
  apt-get install -y --no-install-recommends $packages
fi

# ---- instaladores que não são apt -----------------------------------------
installers=$(python3 -c "
import json
m = json.load(open('$MANIFEST'))
out = [m['apps'][a]['installer'] for a in '$apps'.split() if 'installer' in m['apps'][a]]
print(' '.join(out))
")

for i in $installers; do
  case "$i" in
    awscli-v2)
      # Os MESMOS --bin-dir/--install-dir que aw-app-aws/scripts/install_aws.sh
      # usa. Instalar em outro lugar faria a guarda `command -v aws` falhar e o
      # instalador rodar assim mesmo — o bake viraria peso morto.
      echo "bake: awscli v2"
      arch="$(uname -m)"; [ "$arch" = "arm64" ] && arch="aarch64"
      tmp="$(mktemp -d)"
      curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-${arch}.zip" -o "$tmp/awscliv2.zip"
      python3 -m zipfile -e "$tmp/awscliv2.zip" "$tmp"
      chmod +x "$tmp/aws/install" "$tmp/aws/dist/aws" 2>/dev/null || true
      "$tmp/aws/install" --bin-dir /usr/local/bin --install-dir /usr/local/aws-cli --update
      rm -rf "$tmp"
      ;;
    *)
      echo "bake: instalador desconhecido '$i'" >&2
      exit 1
      ;;
  esac
done

rm -rf /var/lib/apt/lists/*
echo "bake: perfil '$PROFILE' concluído"
