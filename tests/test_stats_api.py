"""Storage figures, site metrics and the additional embedding routes."""
from pathlib import Path

import pytest


def _ready_ad(admin_client, app_env, title="A", target="https://example.com/x",
              size=(300, 100)):
    from PIL import Image
    from app import db
    from app.worker import claim_job, process

    src = Path(app_env) / f"{title}.png"
    Image.new("RGB", size, (10, 90, 200)).save(src)
    with src.open("rb") as fh:
        admin_client.post("/ads", data={"title": title, "target_url": target},
                          files={"file": (f"{title}.png", fh, "image/png")})
    with db.session() as conn:
        process(conn, claim_job(conn))
        return conn.execute("SELECT * FROM ads ORDER BY id DESC LIMIT 1").fetchone()["slug"]


# ---------------- storage ----------------

def test_storage_numbers_are_consistent(app_env):
    from app import stats
    s = stats.storage()
    assert s["total"] > 0
    assert s["used"] + s["free"] <= s["total"]
    assert 0 <= s["used_pct"] <= 100
    # Every segment must be labelled -- the palette validator flagged one fill
    # below 3:1 contrast, which is only allowed with visible labels.
    assert [seg["label"] for seg in s["segments"]] == ["Originals", "Delivered", "Other"]
    assert all(seg["h"] for seg in s["segments"])


def test_storage_segments_never_exceed_the_disk(app_env, admin_client):
    from app import stats
    _ready_ad(admin_client, app_env, title="large", size=(1200, 400))
    s = stats.storage()
    assert sum(seg["bytes"] for seg in s["segments"]) <= s["total"]


def test_human_readable_sizes():
    from app import stats
    assert stats.human(0) == "0 B"
    assert stats.human(1536) == "1.5 kB"
    assert stats.human(5 * 1024**3) == "5.0 GB"


# ---------------- site metrics ----------------

@pytest.mark.parametrize("referer,expected", [
    ("https://www.example-shop.de/page?a=1", "example-shop.de"),
    ("http://blog.example.org/", "blog.example.org"),
    ("https://customer.example:8443/x", "customer.example"),
    ("https://8.8.8.8/x", "8.8.8.8"),           # a public IP counts
    # 203.0.113.x is the RFC 5737 documentation net, and Python treats it as private
    ("https://203.0.113.7/x", None),
    ("", None),
    ("nonsense", None),
    ("https://localhost/x", None),
    ("http://127.0.0.1:8099/", None),           # a test page
    ("http://192.168.1.5/", None),              # a home network
    ("http://10.1.2.3:8080/", None),
    ("https://nas.local/x", None),
])
def test_domain_extraction(referer, expected):
    from app import stats
    assert stats.domain_of(referer) == expected


def test_our_own_host_is_not_a_customer(app_env):
    """The iframe route sets a referer pointing back at us.

    Counting it would let the service appear in its own list of sites -- which
    is exactly what happened on the first live test.
    """
    from app import stats
    own = stats.own_host()
    assert own, "PUBLIC_URL must have a host"
    assert stats.domain_of(f"https://{own}/frame/abc123def456") is None
    assert stats.domain_of(f"https://{own}:443/x") is None
    # A DIFFERENT domain must not be affected
    assert stats.domain_of(f"https://not-{own}/x") == f"not-{own}"


def test_sites_are_counted_even_when_the_impression_is_deduplicated(admin_client, app_env):
    """The whole reason `sites` is its own table.

    Impressions dedupe per visitor and day. If site counting rode along on
    that, the second page a returning visitor sees would never show up.
    """
    from app import db
    slug = _ready_ad(admin_client, app_env, title="dedup")

    admin_client.post("/api/impression", content=slug,
                      headers={"referer": "https://site-one.example/a"})
    admin_client.post("/api/impression", content=slug,
                      headers={"referer": "https://site-two.example/b"})
    admin_client.post("/api/impression", content=slug,
                      headers={"referer": "https://site-one.example/c"})

    with db.session() as conn:
        impressions = conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE kind='impression'").fetchone()["n"]
        rows = {r["domain"]: r["hits"] for r in conn.execute("SELECT domain, hits FROM sites")}

    assert impressions == 1, "deduplication must still apply"
    assert rows == {"site-one.example": 2, "site-two.example": 1}


def test_metrics_totals(admin_client, app_env):
    from app import db, stats
    slug = _ready_ad(admin_client, app_env, title="m")
    admin_client.post("/api/impression", content=slug,
                      headers={"referer": "https://customer.example/"})
    admin_client.get(f"/c/{slug}", headers={"referer": "https://customer.example/"},
                     follow_redirects=False)
    with db.session() as conn:
        m = stats.metrics(conn)
    assert m["impressions"] == 1 and m["clicks"] == 1
    assert m["sites"] == 1 and m["ctr"] == 100.0
    assert m["top_sites"][0]["domain"] == "customer.example"


def test_metrics_survive_an_empty_database(app_env):
    from app import db, stats
    db.migrate()
    with db.session() as conn:
        m = stats.metrics(conn)
    assert m == {"impressions": 0, "clicks": 0, "ctr": 0.0, "sites": 0,
                 "ads_total": 0, "ads_active": 0, "top_sites": []}


# ---------------- additional embedding routes ----------------

def test_img_route_serves_and_counts(admin_client, app_env):
    """The JavaScript-free path: the delivery itself is the impression."""
    from app import db
    slug = _ready_ad(admin_client, app_env, title="pixel")
    r = admin_client.get(f"/img/{slug}", headers={"referer": "https://forum.example/t/1"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/webp"
    assert r.headers["access-control-allow-origin"] == "*"
    # Without no-store a cached <img> would never count again.
    assert "no-store" in r.headers["cache-control"]
    with db.session() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM events WHERE kind='impression'").fetchone()["n"]
        d = conn.execute("SELECT domain FROM sites").fetchone()["domain"]
    assert n == 1 and d == "forum.example"


def test_json_api_shape(admin_client, app_env):
    slug = _ready_ad(admin_client, app_env, title="json")
    r = admin_client.get(f"/api/ad/{slug}")
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "*"
    d = r.json()
    for key in ("slug", "kind", "width", "height", "aspect_ratio", "media_url",
                "click_url", "pixel_url", "frame_url", "script_url", "has_target"):
        assert key in d, f"{key} missing from the JSON response"
    assert d["slug"] == slug
    assert d["media_url"].endswith(f"/m/{slug}")
    assert d["aspect_ratio"] == 3.0

    listing = admin_client.get("/api/ads").json()
    assert listing["count"] == 1 and listing["ads"][0]["slug"] == slug


def test_json_api_random_and_unknown(admin_client, app_env):
    _ready_ad(admin_client, app_env, title="r")
    assert admin_client.get("/api/ad/random").status_code == 200
    assert admin_client.get("/api/ad/doesnotexist").status_code == 404


def test_snippet_options_are_applied(admin_client, app_env):
    slug = _ready_ad(admin_client, app_env, title="opt")
    r = admin_client.get(f"/embed/{slug}.js?width=300&align=left&radius=12")
    assert r.status_code == 200
    assert "var MAXW = 300" in r.text
    assert 'ALIGN = "left"' in r.text
    assert "RADIUS = 12" in r.text
    # A nonsense align value must not get through
    r2 = admin_client.get(f"/embed/{slug}.js?align=javascript:alert(1)")
    assert 'ALIGN = "center"' in r2.text


def test_api_docs_page_renders(admin_client, app_env):
    _ready_ad(admin_client, app_env, title="doc")
    r = admin_client.get("/api")
    assert r.status_code == 200
    assert "MaxAds API" in r.text
    assert "/embed/" in r.text and "/img/" in r.text


def test_dashboard_shows_storage_and_metrics(admin_client, app_env):
    _ready_ad(admin_client, app_env, title="dash")
    r = admin_client.get("/")
    assert r.status_code == 200
    for text in ("Space on the data disk", "Impressions", "Clicks", "Sites", "MaxAds"):
        assert text in r.text, f"{text!r} missing from the dashboard"


# ---------------- frugality ----------------

def test_worker_waits_on_the_pipe_instead_of_polling(app_env, tmp_path, monkeypatch):
    """The worker must sleep until woken, not spin on a timer."""
    import time
    from app import config, worker

    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(config, "WAKEUP_FIFO", tmp_path / "wakeup")

    fd = worker.open_wakeup()
    assert fd is not None, "the wake-up pipe must be created"

    # Without a wake-up it runs into the timeout -- and really waits.
    start = time.monotonic()
    assert worker.wait_for_work(fd, 0.3) == "timeout"
    assert time.monotonic() - start >= 0.25, "it did not really wait"

    # With a wake-up it returns at once.
    import os
    os.write(fd, b"1")
    start = time.monotonic()
    assert worker.wait_for_work(fd, 5) == "wakeup"
    assert time.monotonic() - start < 1.0, "the wake-up did not get through"
    os.close(fd)


def test_upload_wakes_the_worker(admin_client, app_env, tmp_path, monkeypatch):
    """An upload must not wait for the fallback timer."""
    import os
    from PIL import Image
    from app import config, worker

    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(config, "WAKEUP_FIFO", tmp_path / "wakeup")
    fd = worker.open_wakeup()

    src = Path(app_env) / "wake.png"
    Image.new("RGB", (200, 80), (0, 0, 0)).save(src)
    with src.open("rb") as fh:
        admin_client.post("/ads", data={"title": "wake"},
                          files={"file": ("wake.png", fh, "image/png")})

    assert worker.wait_for_work(fd, 2) == "wakeup", "the upload did not wake the worker"
    os.close(fd)


def test_wake_up_never_breaks_an_upload(admin_client, app_env, monkeypatch, tmp_path):
    """If the worker is not running, uploading must still succeed."""
    from PIL import Image
    from app import config, db

    # Points nowhere -- nobody is listening.
    monkeypatch.setattr(config, "WAKEUP_FIFO", tmp_path / "missing" / "wakeup")

    src = Path(app_env) / "nowake.png"
    Image.new("RGB", (200, 80), (0, 0, 0)).save(src)
    with src.open("rb") as fh:
        r = admin_client.post("/ads", data={"title": "nowake"},
                              files={"file": ("nowake.png", fh, "image/png")},
                              follow_redirects=False)
    assert r.status_code == 303, "a failed wake-up must not fail the upload"
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"] == 1


def test_logs_are_buffered_not_written_line_by_line(app_env):
    """Every log line hitting the disk would be the one routine that keeps the
    disk busy. INFO is collected, WARNING goes through at once."""
    import logging
    from app import config

    handler = config.file_handler("buffer.log")
    logger = logging.getLogger("test.buffer")
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    target = config.LOG_DIR / "buffer.log"

    for i in range(50):
        logger.info("line %d", i)
    size_after_info = target.stat().st_size if target.exists() else 0
    assert size_after_info == 0, "INFO lines must not be on disk yet"

    logger.warning("something is broken")
    assert target.exists() and target.stat().st_size > 0, "WARNING must go through at once"
    assert "something is broken" in target.read_text()
    handler.close()


def test_sigterm_ends_the_wait_immediately(app_env, tmp_path, monkeypatch):
    """The worker must stop on SIGTERM, not sit out its timeout.

    Setting a flag in the handler is not enough: since PEP 475 Python restarts
    an interrupted select() by itself, so the loop would sleep on. systemd then
    kills the worker after 90 s -- and because it restarts both units as one
    transaction, the web service stays down that whole time. That is exactly
    what happened in production once (90 s outage per deploy).
    """
    import os
    import signal
    import threading
    import time
    from app import config, worker

    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(config, "WAKEUP_FIFO", tmp_path / "wakeup")

    fd = worker.open_wakeup()
    monkeypatch.setattr(worker, "_wake_fd", fd)
    monkeypatch.setattr(worker, "_running", True)

    previous = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, worker._stop)
    try:
        threading.Timer(0.3, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
        start = time.monotonic()
        # A long timeout -- without the fix it would wait all of it.
        worker.wait_for_work(fd, 30)
        duration = time.monotonic() - start
    finally:
        signal.signal(signal.SIGTERM, previous)
        os.close(fd)

    assert duration < 3, f"SIGTERM did not wake select() (waited {duration:.1f}s)"
    assert worker._running is False, "the stop request was not recorded"
