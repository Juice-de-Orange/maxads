"""End-to-end behaviour that must not regress."""
from pathlib import Path


def test_healthz_reports_ok(client):
    r = client.get("/healthz")
    # ffmpeg may be absent on a dev box; then it must say so rather than lie.
    assert r.status_code in (200, 503)
    if r.status_code == 503:
        assert "missing-ffmpeg" in r.text or "missing-ffprobe" in r.text


def test_dashboard_requires_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_first_admin_is_created_once(app_env):
    from app import auth, db
    db.migrate()
    with db.session() as conn:
        first = auth.ensure_first_admin(conn)
        second = auth.ensure_first_admin(conn)
    assert first is not None and first[0] == "admin"
    assert second is None, "a second admin must not be created"


def test_generated_admin_password_never_reaches_the_logger(app_env, capsys, caplog):
    """The log files live on the data disk and outlast the first login, so the
    generated password goes to stderr once and never through `logging`."""
    import logging
    from fastapi.testclient import TestClient
    from app.main import create_app

    with caplog.at_level(logging.INFO):
        TestClient(create_app())
    err = capsys.readouterr().err
    assert "First admin created" in err, "the generated password must be shown once on stderr"
    password = err.split("password '", 1)[1].split("'", 1)[0]

    assert "first admin 'admin' created" in caplog.text
    assert password not in caplog.text, "the generated password must not be logged"


def test_generated_admin_must_change_password_first(client):
    import re
    from app import db

    with db.session() as conn:
        row = conn.execute("SELECT must_change_password FROM users WHERE username='admin'").fetchone()
    assert row["must_change_password"] == 1

    # Log in with a known password for the generated admin.
    from app import auth
    with db.session() as conn:
        conn.execute("UPDATE users SET password=? WHERE username='admin'",
                     (auth.hash_password("temporary horse battery"),))
    r = client.post("/login", data={"username": "admin", "password": "temporary horse battery"},
                    follow_redirects=False)
    assert r.headers["location"] == "/password"
    # Nothing else works until the password is changed.
    assert client.get("/", follow_redirects=False).headers["location"] == "/password"
    assert client.post("/users", data={"username": "x", "password": "y" * 10},
                       follow_redirects=False).headers["location"] == "/password"

    r = client.post("/password", data={"current": "temporary horse battery", "new": "short",
                                       "confirm": "short"})
    assert r.status_code == 400
    r = client.post("/password", data={"current": "wrong-wrong-wrong", "new": "a-new-password",
                                       "confirm": "a-new-password"})
    assert r.status_code == 401
    r = client.post("/password", data={"current": "temporary horse battery", "new": "a-new-password",
                                       "confirm": "a-new-password"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert client.get("/", follow_redirects=False).status_code == 200
    assert re.search(r"Hey <strong>admin</strong>", client.get("/").text)


def test_admin_password_from_the_environment_is_kept(app_env, monkeypatch, capsys):
    monkeypatch.setenv("MAXADS_ADMIN_PASSWORD", "chosen-by-the-operator")
    from fastapi.testclient import TestClient
    from app.main import create_app

    client = TestClient(create_app())
    assert "First admin created" not in capsys.readouterr().err
    r = client.post("/login", data={"username": "admin", "password": "chosen-by-the-operator"},
                    follow_redirects=False)
    assert r.headers["location"] == "/", "an operator-chosen password needs no forced change"


def test_no_public_registration(client):
    # The brief allows dashboard-created users only -- a /register route
    # would let any stranger take over the service.
    assert client.get("/register").status_code == 404
    assert client.post("/users", data={"username": "x", "password": "y" * 10},
                       follow_redirects=False).status_code == 303


def test_login_rejects_wrong_password(client):
    from app import auth, db
    with db.session() as conn:
        auth.create_user(conn, "someone", "the-right-password")
    r = client.post("/login", data={"username": "someone", "password": "wrong"})
    assert r.status_code == 401


def test_failed_login_does_not_log_the_client_address(client, caplog):
    """The service promises not to store IP addresses. The log files are storage
    too, so a failed login records the username and nothing about the peer."""
    import logging

    with caplog.at_level(logging.INFO):
        r = client.post("/login", data={"username": "someone", "password": "wrong"},
                        headers={"x-forwarded-for": "203.0.113.77"})
    assert r.status_code == 401
    assert "failed login for 'someone'" in caplog.text
    assert "203.0.113.77" not in caplog.text, "the forwarded client IP must not be logged"
    assert "testclient" not in caplog.text, "the peer address must not be logged"


def test_image_upload_creates_job(admin_client, app_env):
    from PIL import Image
    from app import db

    src = Path(app_env) / "input.png"
    Image.new("RGB", (900, 300), (200, 30, 30)).save(src)

    with src.open("rb") as fh:
        r = admin_client.post("/ads", data={"title": "Testbanner",
                                            "target_url": "https://example.com"},
                              files={"file": ("input.png", fh, "image/png")},
                              follow_redirects=False)
    assert r.status_code == 303

    with db.session() as conn:
        ad = conn.execute("SELECT * FROM ads").fetchone()
        job = conn.execute("SELECT * FROM jobs").fetchone()
    assert ad["kind"] == "image"
    assert ad["status"] == "processing"
    assert job["ad_id"] == ad["id"] and job["state"] == "pending"
    assert Path(ad["raw_path"]).exists(), "original must be kept in data/raw"


def test_rejects_unsupported_type(admin_client):
    r = admin_client.post("/ads", data={"title": "x"},
                          files={"file": ("evil.exe", b"MZ", "application/octet-stream")})
    assert r.status_code == 400


def test_embed_snippet_is_javascript_and_open_to_any_origin(admin_client, app_env):
    from app import db
    from app.worker import claim_job, process

    from PIL import Image
    src = Path(app_env) / "in.png"
    Image.new("RGB", (600, 200), (10, 90, 200)).save(src)
    with src.open("rb") as fh:
        admin_client.post("/ads", data={"title": "Banner", "target_url": "https://example.com"},
                          files={"file": ("in.png", fh, "image/png")})

    with db.session() as conn:
        job = claim_job(conn)
        process(conn, job)
        ad = conn.execute("SELECT * FROM ads").fetchone()

    assert ad["status"] == "ready"
    assert ad["media_path"].endswith(".webp")
    assert Path(ad["media_path"]).exists()

    r = admin_client.get(f"/embed/{ad['slug']}.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/javascript")
    assert r.headers["access-control-allow-origin"] == "*"
    # The aspect ratio is what keeps a foreign page from jumping.
    assert "aspect-ratio:" in r.text
    assert ad["slug"] in r.text


def test_impressions_are_deduplicated_per_day(admin_client, app_env):
    from app import db
    from app.worker import claim_job, process
    from PIL import Image

    src = Path(app_env) / "d.png"
    Image.new("RGB", (300, 100), (0, 0, 0)).save(src)
    with src.open("rb") as fh:
        admin_client.post("/ads", data={"title": "D"},
                          files={"file": ("d.png", fh, "image/png")})
    with db.session() as conn:
        process(conn, claim_job(conn))
        slug = conn.execute("SELECT slug FROM ads").fetchone()["slug"]

    for _ in range(5):
        assert admin_client.post("/api/impression", content=slug).status_code == 204

    with db.session() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE kind='impression'"
        ).fetchone()["n"]
    assert n == 1, "a reload must not inflate the impression count"


def test_click_redirects_and_counts(admin_client, app_env):
    from app import db
    from app.worker import claim_job, process
    from PIL import Image

    src = Path(app_env) / "c.png"
    Image.new("RGB", (300, 100), (0, 0, 0)).save(src)
    with src.open("rb") as fh:
        admin_client.post("/ads", data={"title": "C", "target_url": "https://example.org/x"},
                          files={"file": ("c.png", fh, "image/png")})
    with db.session() as conn:
        process(conn, claim_job(conn))
        slug = conn.execute("SELECT slug FROM ads").fetchone()["slug"]

    r = admin_client.get(f"/c/{slug}", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "https://example.org/x"

    with db.session() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM events WHERE kind='click'").fetchone()["n"]
    assert n == 1


def test_inactive_ad_is_not_delivered(admin_client, app_env):
    from app import db
    from app.worker import claim_job, process
    from PIL import Image

    src = Path(app_env) / "i.png"
    Image.new("RGB", (300, 100), (0, 0, 0)).save(src)
    with src.open("rb") as fh:
        admin_client.post("/ads", data={"title": "I"},
                          files={"file": ("i.png", fh, "image/png")})
    with db.session() as conn:
        process(conn, claim_job(conn))
        slug = conn.execute("SELECT slug FROM ads").fetchone()["slug"]

    admin_client.post(f"/ads/{slug}/toggle", follow_redirects=False)
    assert admin_client.get(f"/m/{slug}").status_code == 404
    assert "unknown or inactive" in admin_client.get(f"/embed/{slug}.js").text


def test_visitor_hash_hides_the_ip(app_env):
    from app import auth
    h = auth.visitor_hash("203.0.113.9", "Mozilla/5.0", "salt")
    assert "203.0.113.9" not in h
    assert h == auth.visitor_hash("203.0.113.9", "Mozilla/5.0", "salt"), "stable within a day"
    assert h != auth.visitor_hash("203.0.113.10", "Mozilla/5.0", "salt")


def test_video_output_is_never_larger_than_an_already_web_ready_original(
        app_env, monkeypatch, tmp_path):
    """A delivered ad must not cost more bytes than what was uploaded.

    The condition matters: it only holds when the original is already playable
    in a browser. For a yuv444p original, browser compatibility wins over size --
    see test_delivered_video_is_always_browser_decodable.
    """
    import shutil
    import subprocess
    if not shutil.which("ffmpeg"):
        import pytest
        pytest.skip("ffmpeg not available")

    from app import media

    src = tmp_path / "src.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
        "-i", "testsrc=size=640x360:rate=25", "-t", "2",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src),
    ], check=True)
    assert media._is_web_ready(src), "precondition: the original is already web-ready"

    result = media.process_video(src, "sizecheck")
    assert result.media_path.exists()
    assert result.media_path.stat().st_size <= src.stat().st_size, (
        "transcoding must not inflate an already web-ready clip"
    )
    assert result.thumb_path.exists()


def test_delivered_video_is_always_browser_decodable(app_env, tmp_path):
    """The bug this guards against is silent: an H.264 file in yuv444p plays in
    VLC and hangs forever in Chrome -- readyState stays 0, no error fires.

    ffmpeg's testsrc produces exactly that by default, and it compresses so well
    that the size check would otherwise hand the original straight through.
    """
    import shutil
    import subprocess
    if not shutil.which("ffmpeg"):
        import pytest
        pytest.skip("ffmpeg not available")

    from app import media

    src = tmp_path / "yuv444.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
        "-i", "testsrc=size=640x360:rate=25", "-t", "2",
        "-c:v", "libx264", "-pix_fmt", "yuv444p", str(src),
    ], check=True)

    # Precondition: the original really is the undecodable kind.
    assert not media._is_web_ready(src), "yuv444p must not count as web-ready"

    result = media.process_video(src, "pixfmt")
    out = subprocess.run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=pix_fmt", "-of", "csv=p=0", str(result.media_path),
    ], capture_output=True, text=True, check=True).stdout.strip()
    assert out in media.BROWSER_PIXEL_FORMATS, f"delivered pix_fmt {out!r} is not decodable"
