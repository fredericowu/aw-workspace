"""``aw-workspace-cli update`` — AW_BACKEND_URL/AW_WORKSPACE resolve through
the same ``.env`` fallback ``core_restart._env`` uses, so this command only
ever fails on the thing that's actually the gate: the missing central
-identity token. See card 3d15bf3b-9510-816a-bff8-fc6698619fa4.
"""
from __future__ import annotations

from src.cli.commands import update as update_cmd


def test_fails_on_backend_url_when_unresolvable_anywhere(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("AW_BACKEND_URL", raising=False)
    monkeypatch.delenv("AW_WORKSPACE", raising=False)
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path))

    assert update_cmd.run(["workspace"]) == 1
    assert "AW_BACKEND_URL and AW_WORKSPACE could not be resolved" in capsys.readouterr().out


def test_backend_url_and_workspace_fall_back_to_dot_env(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("AW_BACKEND_URL", raising=False)
    monkeypatch.delenv("AW_WORKSPACE", raising=False)
    monkeypatch.delenv("AW_ID_TOKEN", raising=False)
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path))
    (tmp_path / ".env").write_text(
        "AW_BACKEND_URL=http://backend.example\nAW_WORKSPACE=my-slug\n"
    )

    # Resolving backend_url/workspace via .env should get us past that check
    # and fail on the token instead — the actual gate.
    assert update_cmd.run(["workspace"]) == 1
    out = capsys.readouterr().out
    assert "AW_BACKEND_URL and AW_WORKSPACE could not be resolved" not in out
    assert "no identity token" in out


def test_missing_token_error_points_at_restart_core(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("AW_ID_TOKEN", raising=False)
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path))
    (tmp_path / ".env").write_text(
        "AW_BACKEND_URL=http://backend.example\nAW_WORKSPACE=my-slug\n"
    )

    assert update_cmd.run(["workspace"]) == 1
    assert "restart core" in capsys.readouterr().out


def test_successful_update_posts_with_bearer_token(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path))
    monkeypatch.setenv("AW_BACKEND_URL", "http://backend.example")
    monkeypatch.setenv("AW_WORKSPACE", "my-slug")

    calls = []

    class _FakeResponse:
        status_code = 200

        def json(self):
            return {"status": "triggered"}

    def fake_post(url, headers=None, timeout=None):
        calls.append((url, headers))
        return _FakeResponse()

    monkeypatch.setattr(update_cmd.httpx, "post", fake_post)

    assert update_cmd.run(["workspace", "--token", "abc123"]) == 0
    assert calls == [("http://backend.example/api/workspaces/my-slug/update",
                       {"Authorization": "Bearer abc123"})]
    assert "update triggered" in capsys.readouterr().out
