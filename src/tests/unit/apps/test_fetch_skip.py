"""O reconciler confia no disco quando a versão bate (fetch-skip).

Medido no host aw em 2026-10-09: TODO boot re-buscava TODA app. O único
skip no caminho é ``already-loaded``, que é in-process e portanto vazio num
processo novo. E isso não é só download desperdiçado — ``fetch_app_repo``
troca o diretório do pacote por ``os.replace``, levando junto o ``.data``
da própria app. O venv do google-workspace-mcp (246s) e o bundle do
codegraph (98s) eram reconstruídos do zero a cada boot, ambos atrás do
flock global de instalador, atrasando toda app enfileirada.

É também o que torna possível entregar uma app JÁ instalada na imagem: um
workspace novo é semeado da imagem por ``podman cp``
(aw-remote-host, bootstrap/workspace/install.sh), e sem esta checagem o
primeiro reconcile jogaria esse seed fora e baixaria tudo de novo.

A regra é **por versão, não por presença** — manter um ``.data`` construído
para a v1 servindo a v2 é pior que reconstruir.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from src.apps.reconciler import AppSpec, Reconciler


@pytest.fixture
def apps_root(tmp_path, monkeypatch) -> pathlib.Path:
    root = tmp_path / "apps"
    root.mkdir()
    monkeypatch.setenv("AW_APPS_ROOT", str(root))
    return root


def _install(apps_root: pathlib.Path, slug: str, version: str) -> pathlib.Path:
    pkg = apps_root / slug
    (pkg / ".data").mkdir(parents=True, exist_ok=True)
    (pkg / ".data" / "venv-marker").write_text("caro de construir")
    (pkg / "aw-app.json").write_text(json.dumps(
        {"manifest_version": 1, "id": slug, "name": slug, "version": version}))
    return pkg


class _Recorder:
    """Substitui fetch_app_repo e registra se foi chamado."""

    def __init__(self, apps_root: pathlib.Path):
        self.calls: list[tuple] = []
        self._root = apps_root

    def __call__(self, repo, ref, *, slug, **kw):
        self.calls.append((repo, ref, slug))
        # Imita o rename-swap: o dir inteiro é substituído, .data inclusive.
        pkg = _install(self._root, slug, str(ref).lstrip("v"))
        (pkg / ".data" / "venv-marker").unlink(missing_ok=True)
        return str(pkg)


def _bare(fetch) -> Reconciler:
    """Só _resolve_package_dir é exercitado aqui; o runtime não é tocado."""
    r = Reconciler.__new__(Reconciler)
    r._fetch = fetch
    return r


def _spec(slug: str, ref: str) -> AppSpec:
    return AppSpec(app_id=slug, repo=f"tekflox/aw-app-{slug}", ref=ref)


def test_same_version_on_disk_is_not_refetched(apps_root):
    _install(apps_root, "demo", "0.13.0")
    fetch = _Recorder(apps_root)

    got = _bare(fetch)._resolve_package_dir(_spec("demo", "v0.13.0"))

    assert fetch.calls == [], "re-buscou uma app que já estava na versão certa"
    assert got == str(apps_root / "demo")


def test_the_apps_own_data_survives_the_skip(apps_root):
    """O ponto inteiro: venvs e bundles pré-construídos continuam lá."""
    _install(apps_root, "demo", "0.13.0")
    fetch = _Recorder(apps_root)

    _bare(fetch)._resolve_package_dir(_spec("demo", "v0.13.0"))

    assert (apps_root / "demo" / ".data" / "venv-marker").exists()


def test_a_different_version_still_fetches_and_wipes_data(apps_root):
    """A outra metade da regra. Um .data construído para a versão antiga não
    pode sobreviver a uma troca de versão — reconstruir é mais barato que
    servir um venv errado."""
    _install(apps_root, "demo", "0.12.3")
    fetch = _Recorder(apps_root)

    _bare(fetch)._resolve_package_dir(_spec("demo", "v0.13.0"))

    assert len(fetch.calls) == 1
    assert not (apps_root / "demo" / ".data" / "venv-marker").exists()


def test_nothing_on_disk_fetches(apps_root):
    fetch = _Recorder(apps_root)
    _bare(fetch)._resolve_package_dir(_spec("demo", "v0.13.0"))
    assert len(fetch.calls) == 1


def test_an_unreadable_manifest_is_treated_as_absent(apps_root):
    """O único estado que o rename atômico do fetch_app_repo não descarta.
    Confiar nele deixaria um diretório corrompido confiado para sempre."""
    pkg = apps_root / "demo"
    pkg.mkdir()
    (pkg / "aw-app.json").write_text("{ isto não é json")
    fetch = _Recorder(apps_root)

    _bare(fetch)._resolve_package_dir(_spec("demo", "v0.13.0"))

    assert len(fetch.calls) == 1


def test_a_manifest_without_a_version_is_treated_as_absent(apps_root):
    pkg = apps_root / "demo"
    pkg.mkdir()
    (pkg / "aw-app.json").write_text(json.dumps({"id": "demo"}))
    fetch = _Recorder(apps_root)

    _bare(fetch)._resolve_package_dir(_spec("demo", "v0.13.0"))

    assert len(fetch.calls) == 1


@pytest.mark.parametrize("ref", ["HEAD", "main", "feature/x", "a1b2c3d"])
def test_a_non_version_ref_always_fetches(apps_root, ref):
    """Um branch ou sha não carrega versão para comparar. Casar por presença
    aqui congelaria um checkout de dev para sempre."""
    _install(apps_root, "demo", "0.13.0")
    fetch = _Recorder(apps_root)

    _bare(fetch)._resolve_package_dir(_spec("demo", ref))

    assert len(fetch.calls) == 1, f"ref {ref!r} deveria sempre buscar"


def test_package_dir_specs_are_untouched(tmp_path):
    """Uma app sideloaded (dev checkout via POST /api/apps/install) não tem
    repo e nunca passou pelo fetch — este caminho não pode mudar."""
    pkg = tmp_path / "sideloaded"
    pkg.mkdir()
    fetch = _Recorder(tmp_path)
    spec = AppSpec(app_id="demo", package_dir=str(pkg))

    assert _bare(fetch)._resolve_package_dir(spec) == str(pkg)
    assert fetch.calls == []
