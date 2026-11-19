"""``doctor`` on a slow server must say so, not die with a traceback."""
from __future__ import annotations

import httpx

from src.cli.commands import doctor as doctor_mod


def test_doctor_reports_unanswered_server_cleanly(monkeypatch, capsys):
    def boom(*a, **k):
        raise httpx.ReadTimeout("")

    monkeypatch.setattr(doctor_mod.local_client, "request", boom)
    monkeypatch.setattr(doctor_mod.local_client.time, "sleep", lambda s: None)
    assert doctor_mod.run([]) == 2
    assert "did not answer" in capsys.readouterr().out
