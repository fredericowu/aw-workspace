"""``x-generate``: per-install secrets an app owns both ends of.

See CONFIG_GENERATORS in src/apps/manifest.py for why `default` cannot do this.
"""
import pytest

from src.apps.manifest import ManifestError, validate_manifest


def _manifest(**over):
    base = {"manifest_version": 1, "id": "demo", "name": "Demo", "version": "1.0.0",
            "tier": "inprocess", "runtime": {"entrypoint": "demo:Plugin"}}
    base.update(over)
    return base


def _schema(**props):
    return {"type": "object", "properties": props}


def test_generated_config_mints_a_value_when_none_is_set():
    m = validate_manifest(_manifest(config_schema=_schema(
        db_password={"type": "string", "x-secret": True, "x-generate": "urlsafe32"})))
    minted = m.generated_config({})
    assert set(minted) == {"db_password"}
    assert len(minted["db_password"]) > 20


def test_two_installs_do_not_get_the_same_secret():
    m = validate_manifest(_manifest(config_schema=_schema(
        db_password={"type": "string", "x-generate": "urlsafe32"})))
    assert m.generated_config({})["db_password"] != m.generated_config({})["db_password"]


def test_an_existing_value_is_never_regenerated():
    # Rotating a MySQL password on reinstall leaves the existing datadir
    # unopenable — this is the property that matters most here.
    m = validate_manifest(_manifest(config_schema=_schema(
        db_password={"type": "string", "x-generate": "urlsafe32"})))
    assert m.generated_config({"db_password": "already-set"}) == {}


def test_an_empty_string_still_counts_as_missing():
    m = validate_manifest(_manifest(config_schema=_schema(
        db_password={"type": "string", "x-generate": "urlsafe32"})))
    assert "db_password" in m.generated_config({"db_password": ""})


def test_fields_without_x_generate_are_left_alone():
    m = validate_manifest(_manifest(config_schema=_schema(
        api_url={"type": "string"})))
    assert m.generated_config({}) == {}


@pytest.mark.parametrize("kind", ["urlsafe32", "urlsafe64", "hex32", "uuid4"])
def test_every_documented_generator_produces_a_value(kind):
    m = validate_manifest(_manifest(config_schema=_schema(
        k={"type": "string", "x-generate": kind})))
    assert m.generated_config({})["k"]


def test_an_unknown_generator_is_rejected_at_install_time():
    with pytest.raises(ManifestError, match="x-generate must be one of"):
        validate_manifest(_manifest(config_schema=_schema(
            k={"type": "string", "x-generate": "rot13"})))


def test_default_and_x_generate_together_are_rejected():
    # The default would win on first install, so the generator would never
    # fire and the app would keep shipping the baked-in credential.
    with pytest.raises(ManifestError, match="cannot set both"):
        validate_manifest(_manifest(config_schema=_schema(
            k={"type": "string", "default": "hunter2", "x-generate": "hex32"})))
