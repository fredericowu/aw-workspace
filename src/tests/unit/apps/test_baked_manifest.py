"""image/baked.json — o manifesto do que a IMAGEM pré-provisiona.

Cobre o que dá para checar sem as apps instaladas: que todo perfil cita apps
declaradas, que todo repo apt referenciado existe, e que todo instalador
não-apt é um que o bake.sh sabe executar. Um erro em qualquer um desses só
apareceria no build da imagem — multi-arch, ~30min — e o build é longe
demais do erro para ser um bom lugar de descobrir.

O QUE ESTE ARQUIVO **NÃO** COBRE, e o modo de falha que fica em aberto: se a
imagem instalar algo que a guarda do instalador da app não reconheça (nome
diferente, versão diferente da que o script pina), a guarda falha, o
instalador roda em runtime assim mesmo, e o bake vira peso morto SILENCIOSO
— ninguém percebe, o boot só continua lento. Fechar isso exige comparar o
manifesto com os scripts reais das apps, que não estão no repo do workspace
(vêm de release). Ver o desenho em docs/knowledge_base.
"""
from __future__ import annotations

import json
import pathlib

import pytest

MANIFEST = pathlib.Path(__file__).resolve().parents[4] / "image" / "baked.json"

#: Os instaladores não-apt que image/bake.sh implementa. Manter em sincronia
#: com o `case "$i" in` de lá — um nome aqui que o bake.sh não conhece falha
#: o build com "instalador desconhecido".
KNOWN_INSTALLERS = {"awscli-v2"}


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_manifest_exists_where_the_dockerfile_copies_it():
    assert MANIFEST.is_file(), f"{MANIFEST} não existe — o Dockerfile faz COPY image/"


def test_every_profile_cites_declared_apps(manifest):
    for profile, apps in manifest["profiles"].items():
        unknown = [a for a in apps if a not in manifest["apps"]]
        assert not unknown, f"perfil {profile!r} cita apps sem entrada em 'apps': {unknown}"


def test_there_is_a_default_profile(manifest):
    """O Dockerfile tem ARG AW_IMAGE_PROFILE=default — sem essa chave, um
    build sem build-arg falha."""
    assert "default" in manifest["profiles"]


def test_every_apt_repo_referenced_is_declared(manifest):
    for app, spec in manifest["apps"].items():
        repo = spec.get("apt_repo")
        if repo is not None:
            assert repo in manifest["apt_repos"], (
                f"app {app!r} referencia o repo apt {repo!r}, que não está em 'apt_repos'"
            )


def test_every_apt_repo_has_the_fields_bake_sh_reads(manifest):
    for name, repo in manifest["apt_repos"].items():
        for field in ("key_url", "keyring", "list", "repo", "key_is_armored"):
            assert field in repo, f"repo apt {name!r} sem o campo {field!r}"
        assert isinstance(repo["key_is_armored"], bool), (
            f"repo apt {name!r}: key_is_armored decide entre `gpg --dearmor` e "
            "gravar a chave direto; errar isso só aparece como NO_PUBKEY no "
            "apt-get update seguinte"
        )


def test_every_installer_is_one_bake_sh_implements(manifest):
    for app, spec in manifest["apps"].items():
        inst = spec.get("installer")
        if inst is not None:
            assert inst in KNOWN_INSTALLERS, (
                f"app {app!r} pede o instalador {inst!r}, que image/bake.sh não "
                f"implementa (conhecidos: {sorted(KNOWN_INSTALLERS)})"
            )


def test_every_app_declares_something_to_install(manifest):
    """Uma entrada sem `apt` nem `installer` é ruído: ela não faz nada e
    sugere, falsamente, que aquela app está coberta pela imagem."""
    for app, spec in manifest["apps"].items():
        assert spec.get("apt") or spec.get("installer"), (
            f"app {app!r} não declara nem 'apt' nem 'installer'"
        )


def test_every_app_says_why_it_is_worth_baking(manifest):
    """A lista precisa resistir a virar acúmulo. Cada entrada custa tamanho
    de imagem, então cada uma tem que carregar o custo medido que justifica
    estar ali — foi assim que awscli e gcloud viraram decisão explícita e
    não default."""
    for app, spec in manifest["apps"].items():
        why = spec.get("why", "")
        assert why.strip(), f"app {app!r} sem 'why' — qual custo medido justifica assar isso?"


def test_nothing_host_mounted_is_declared_bakeable(manifest):
    """A regra que define o arquivo: a imagem só alcança a camada do
    container. Qualquer caminho sob /opt/aw-workspace é bind mount do host —
    apps/ é excluído do syncWorkspaceSource, .aw-workspace/ é estado de
    runtime — então declarar algo de lá aqui seria trabalho jogado fora no
    primeiro recreate."""
    blob = json.dumps({k: v for k, v in manifest["apps"].items()})
    for forbidden in ("/opt/aw-workspace/apps", ".aw-workspace/", "npm install -g"):
        assert forbidden not in blob, (
            f"manifesto declara {forbidden!r}, que vive no host mount (ou vai "
            "para ele) e não pode ser assado na imagem"
        )
