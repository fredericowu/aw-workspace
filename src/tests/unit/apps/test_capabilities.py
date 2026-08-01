"""Unit tests for the capability catalog + trust tier (F2)."""
from __future__ import annotations

import pytest

from src.apps.capabilities import (
    filter_grants,
    is_high_risk,
    is_valid_capability,
)


@pytest.mark.parametrize("cap", [
    "routes:register", "db:own-tables", "commands:install", "service:manage",
    "net:outbound", "fs:workspace-data", "secrets:own", "containers:manage",
    "ui:code", "config:extend:kanban", "ui:slots:core.toolbar",
])
def test_known_capabilities_are_valid(cap):
    assert is_valid_capability(cap)


@pytest.mark.parametrize("cap", [
    "do:anything", "routes", "config:extend:", "ui:slots:", "", "ROUTES:REGISTER",
])
def test_unknown_or_malformed_capabilities_are_invalid(cap):
    assert not is_valid_capability(cap)


@pytest.mark.parametrize("cap", ["ui:code", "containers:manage", "config:extend:kanban"])
def test_high_risk_caps(cap):
    assert is_high_risk(cap)


@pytest.mark.parametrize("cap", ["routes:register", "secrets:own", "ui:slots:core.toolbar"])
def test_low_risk_caps(cap):
    assert not is_high_risk(cap)


def test_unsigned_app_keeps_high_risk_caps():
    """Signature/trust gating is disabled (Frederico decision 2026-08-01) —
    see filter_grants' docstring. An "unsigned" app is no longer refused
    anything; F2 never had real signing infra to make that distinction
    meaningful, and it was blocking first-party marketplace apps too."""
    granted, refused = filter_grants(
        ["routes:register", "ui:code", "config:extend:kanban"], signed=False)
    assert granted == ["routes:register", "ui:code", "config:extend:kanban"]
    assert refused == []


def test_signed_app_keeps_high_risk_caps():
    granted, refused = filter_grants(["routes:register", "ui:code"], signed=True)
    assert granted == ["routes:register", "ui:code"]
    assert refused == []
