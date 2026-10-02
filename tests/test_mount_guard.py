"""The failure mode this guards against: a missing mount that looks like an
empty directory. The service must refuse to run instead."""
import sys

import pytest


def test_refuses_to_start_without_the_sentinel(tmp_path, monkeypatch):
    base = tmp_path / "maxads"
    base.mkdir()  # exists, but the disk is NOT mounted -- no .mounted file
    monkeypatch.setenv("MAXADS_BASE", str(base))
    monkeypatch.delenv("MAXADS_REQUIRE_MOUNT", raising=False)
    for name in [m for m in list(sys.modules) if m.startswith("app")]:
        del sys.modules[name]

    from app import config
    with pytest.raises(SystemExit) as exc:
        config.require_mount()
    assert "not mounted" in str(exc.value)


def test_healthz_fails_when_the_mount_disappears(client, app_env):
    assert client.get("/healthz").status_code in (200, 503)
    (app_env / ".mounted").unlink()
    r = client.get("/healthz")
    assert r.status_code == 503
    assert "data-not-mounted" in r.text


def test_guard_can_be_switched_off_for_plain_volumes(tmp_path, monkeypatch):
    """A container volume cannot drop out on its own, so the sentinel is optional there."""
    base = tmp_path / "volume"
    base.mkdir()
    monkeypatch.setenv("MAXADS_BASE", str(base))
    monkeypatch.setenv("MAXADS_REQUIRE_MOUNT", "0")
    for name in [m for m in list(sys.modules) if m.startswith("app")]:
        del sys.modules[name]

    from app import config
    config.require_mount()  # must not raise
