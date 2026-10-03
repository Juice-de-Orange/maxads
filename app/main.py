"""MaxAds -- FastAPI application: dashboard, upload and public ad delivery."""
import logging
import os
import random
import re
import secrets
import sys
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response,
)
from fastapi.templating import Jinja2Templates
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from . import auth, config, db, media, stats

log = logging.getLogger("maxads.web")

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))

SLUG_RE = re.compile(r"^[a-z0-9]{12}$")


def create_app() -> FastAPI:
    config.require_mount()
    config.ensure_dirs()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            config.file_handler("web.log"),
            logging.StreamHandler(sys.stdout),
        ],
    )

    db.migrate()
    secret = config.SECRET_KEY or config.load_or_create_secret(
        "MAXADS_SECRET_KEY", config.BASE_DIR / ".secret_key"
    )
    count_salt = config.COUNT_SALT or config.load_or_create_secret(
        "MAXADS_COUNT_SALT", config.BASE_DIR / ".count_salt"
    )

    with db.session() as conn:
        created = auth.ensure_first_admin(conn, config.ADMIN_PASSWORD or None)
    if created:
        username, password, generated = created
        log.warning("first admin %r created", username)
        if generated:
            # Printed once to stderr on purpose and never passed to the logger:
            # the log files live on the data disk and outlast the first login.
            print("=" * 62, file=sys.stderr)
            print(f"First admin created -- username {username!r}, password {password!r}",
                  file=sys.stderr)
            print("You will be asked to change it on the first login.", file=sys.stderr)
            print("=" * 62, file=sys.stderr, flush=True)

    application = FastAPI(title="MaxAds", docs_url=None, redoc_url=None)
    application.state.count_salt = count_salt

    # Ads are embedded on foreign sites, so delivery must be open to any origin.
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )
    # The dashboard session must not ride along on cross-site ad requests.
    application.add_middleware(
        SessionMiddleware, secret_key=secret, same_site="lax", https_only=False,
        session_cookie="maxads_session",
    )
    register_routes(application)
    return application


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def current_user(request: Request) -> dict[str, Any] | None:
    uid = request.session.get("uid")
    if not uid:
        return None
    with db.session() as conn:
        row = conn.execute(
            "SELECT id, username, must_change_password FROM users WHERE id = ?", (uid,)
        ).fetchone()
    return dict(row) if row else None


def require_session(request: Request) -> dict[str, Any]:
    """Any logged-in user, including one who still has to change the password."""
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


def require_user(request: Request) -> dict[str, Any]:
    """A logged-in user who is allowed to work: a generated first password must
    be replaced before anything else is possible."""
    user = require_session(request)
    if user["must_change_password"]:
        raise HTTPException(status_code=303, headers={"Location": "/password"})
    return user


def wake_worker() -> None:
    """Nudge the media worker. Best effort by design.

    If nobody is listening the worker is simply not running -- it will find the
    job on its next pass. A failed wake-up must never fail an upload.
    """
    try:
        fd = os.open(config.WAKEUP_FIFO, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return
    try:
        os.write(fd, b"1")
    except OSError:
        pass
    finally:
        os.close(fd)


def client_ip(request: Request) -> str:
    """Real client IP when running behind a reverse proxy."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "0.0.0.0"


def ad_by_slug(conn, slug: str):
    if not SLUG_RE.match(slug):
        return None
    return conn.execute(
        "SELECT * FROM ads WHERE slug = ? AND status = 'ready' AND active = 1", (slug,)
    ).fetchone()


def record_event(request: Request, conn, ad_id: int, kind: str) -> None:
    """Insert an event, silently ignoring the duplicate within the same day."""
    visitor = auth.visitor_hash(
        client_ip(request), request.headers.get("user-agent", ""),
        request.app.state.count_salt,
    )
    referer = (request.headers.get("referer") or "")[:300]
    conn.execute(
        "INSERT OR IGNORE INTO events(ad_id, kind, day, visitor, referer) "
        "VALUES (?, ?, date('now'), ?, ?)",
        (ad_id, kind, visitor, referer),
    )
    # Deliberately outside the OR IGNORE above: the event may be a duplicate
    # for this visitor and day, the page it ran on is still a real site.
    stats.record_site(conn, ad_id, referer, clicked=(kind == "click"))


def stats_for(conn, ad_id: int) -> dict[str, int]:
    rows = conn.execute(
        "SELECT kind, COUNT(*) AS n FROM events WHERE ad_id = ? GROUP BY kind", (ad_id,)
    ).fetchall()
    counts = {r["kind"]: r["n"] for r in rows}
    return {"impressions": counts.get("impression", 0), "clicks": counts.get("click", 0)}


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------

def register_routes(app: FastAPI) -> None:

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz() -> PlainTextResponse:
        """Liveness plus the two things that silently break: mount and tools."""
        problems = []
        if config.REQUIRE_MOUNT and not config.SENTINEL.exists():
            problems.append("data-not-mounted")
        for name, ok in media.tools_available().items():
            if not ok:
                problems.append(f"missing-{name}")
        try:
            with db.session() as conn:
                conn.execute("SELECT 1 FROM ads LIMIT 1")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"db:{exc}")
        if problems:
            return PlainTextResponse("FAIL " + " ".join(problems), status_code=503)
        return PlainTextResponse("OK")

    # ---------------- auth ----------------

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        if current_user(request):
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(request, "login.html", {"error": None})

    @app.post("/login")
    def login(request: Request, username: str = Form(...), password: str = Form(...)):
        with db.session() as conn:
            user = auth.authenticate(conn, username, password)
        if user is None:
            # Without the client address on purpose: the log files are storage
            # too, and the service promises not to keep IP addresses.
            log.warning("failed login for %r", username)
            return templates.TemplateResponse(
                request, "login.html", {"error": "Wrong username or password."},
                status_code=401,
            )
        request.session["uid"] = user["id"]
        if user["must_change_password"]:
            return RedirectResponse("/password", status_code=303)
        return RedirectResponse("/", status_code=303)

    @app.get("/password", response_class=HTMLResponse)
    def password_form(request: Request, user: dict = Depends(require_session)):
        return templates.TemplateResponse(request, "password.html", {
            "user": user, "error": None, "forced": bool(user["must_change_password"]),
        })

    @app.post("/password")
    def change_password(request: Request, current: str = Form(...), new: str = Form(...),
                        confirm: str = Form(...), user: dict = Depends(require_session)):
        def again(error: str, status: int):
            return templates.TemplateResponse(request, "password.html", {
                "user": user, "error": error, "forced": bool(user["must_change_password"]),
            }, status_code=status)

        if len(new) < 10:
            return again("The new password needs at least 10 characters.", 400)
        if new != confirm:
            return again("The two new passwords do not match.", 400)
        if new == current:
            return again("The new password must differ from the current one.", 400)
        with db.session() as conn:
            if auth.authenticate(conn, user["username"], current) is None:
                return again("The current password is wrong.", 401)
            auth.set_password(conn, user["id"], new)
        log.info("password changed by %s", user["username"])
        return RedirectResponse("/", status_code=303)

    @app.get("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    # ---------------- dashboard ----------------

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, user: dict = Depends(require_user)):
        with db.session() as conn:
            ads = conn.execute("SELECT * FROM ads ORDER BY id DESC").fetchall()
            items = []
            for ad in ads:
                d = dict(ad)
                d.update(stats_for(conn, ad["id"]))
                items.append(d)
            users = conn.execute(
                "SELECT id, username, created_at FROM users ORDER BY id"
            ).fetchall()
            for item in items:
                item["sites"] = stats.sites_for(conn, item["id"])
            metrics = stats.metrics(conn)
        return templates.TemplateResponse(request, "dashboard.html", {
            "user": user, "ads": items, "users": users, "public_url": config.PUBLIC_URL,
            "metrics": metrics, "storage": stats.storage(),
        })

    @app.post("/ads")
    async def upload(request: Request, title: str = Form(...),
                     target_url: str = Form(""), file: UploadFile = File(...),
                     user: dict = Depends(require_user)):
        suffix = Path(file.filename or "").suffix.lower()
        kind = media.kind_for(suffix)
        if kind is None:
            raise HTTPException(400, f"File type {suffix or '?'} is not supported.")

        slug = secrets.token_hex(6)
        raw_path = config.RAW_DIR / f"{slug}{suffix}"

        size = 0
        with raw_path.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > config.MAX_UPLOAD_BYTES:
                    out.close()
                    raw_path.unlink(missing_ok=True)
                    raise HTTPException(413, "File is too large.")
                out.write(chunk)

        with db.session() as conn:
            cur = conn.execute(
                "INSERT INTO ads(slug, title, target_url, kind, raw_path, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (slug, title.strip() or slug, target_url.strip(), kind,
                 str(raw_path), user["id"]),
            )
            conn.execute("INSERT INTO jobs(ad_id) VALUES (?)", (cur.lastrowid,))
        wake_worker()
        log.info("upload %s (%s, %d bytes) by %s", slug, kind, size, user["username"])
        return RedirectResponse("/", status_code=303)

    @app.post("/ads/{slug}/toggle")
    def toggle(request: Request, slug: str, user: dict = Depends(require_user)):
        with db.session() as conn:
            conn.execute("UPDATE ads SET active = 1 - active WHERE slug = ?", (slug,))
        return RedirectResponse("/", status_code=303)

    @app.post("/ads/{slug}/delete")
    def delete_ad(request: Request, slug: str, user: dict = Depends(require_user)):
        with db.session() as conn:
            ad = conn.execute("SELECT * FROM ads WHERE slug = ?", (slug,)).fetchone()
            if ad is None:
                raise HTTPException(404)
            for key in ("raw_path", "media_path", "thumb_path"):
                if ad[key]:
                    Path(ad[key]).unlink(missing_ok=True)
            conn.execute("DELETE FROM ads WHERE id = ?", (ad["id"],))
        log.info("ad %s deleted by %s", slug, user["username"])
        return RedirectResponse("/", status_code=303)

    @app.post("/users")
    def add_user(request: Request, username: str = Form(...), password: str = Form(...),
                 user: dict = Depends(require_user)):
        if len(password) < 10:
            raise HTTPException(400, "The password needs at least 10 characters.")
        with db.session() as conn:
            exists = conn.execute(
                "SELECT 1 FROM users WHERE username = ?", (username.strip(),)
            ).fetchone()
            if exists:
                raise HTTPException(409, "That username is already taken.")
            auth.create_user(conn, username, password, created_by=user["id"])
        log.info("user %r created by %s", username, user["username"])
        return RedirectResponse("/", status_code=303)

    # ---------------- public delivery ----------------

    @app.get("/m/{slug}")
    def serve_media(slug: str, thumb: int = 0):
        with db.session() as conn:
            ad = ad_by_slug(conn, slug)
            if ad is None:
                raise HTTPException(404)
            path = Path(ad["thumb_path"] if thumb else ad["media_path"])
        if not path.exists():
            raise HTTPException(404)
        media_type = "image/webp" if path.suffix == ".webp" else "video/mp4"
        return FileResponse(path, media_type=media_type, headers={
            "Cache-Control": "public, max-age=86400",
            "Access-Control-Allow-Origin": "*",
        })

    @app.get("/c/{slug}")
    def click(request: Request, slug: str):
        with db.session() as conn:
            ad = ad_by_slug(conn, slug)
            if ad is None:
                raise HTTPException(404)
            target = ad["target_url"]
            if target:
                record_event(request, conn, ad["id"], "click")
        if not target:
            return PlainTextResponse("No target URL set for this ad.", status_code=404)
        return RedirectResponse(target, status_code=302)

    @app.post("/api/impression")
    async def impression(request: Request):
        # sendBeacon delivers a plain body, not a form -- read it directly.
        raw = (await request.body()).decode("utf-8", "replace").strip()
        slug = raw[:64]
        with db.session() as conn:
            ad = ad_by_slug(conn, slug)
            if ad is not None:
                record_event(request, conn, ad["id"], "impression")
        return Response(status_code=204, headers={"Access-Control-Allow-Origin": "*"})

    def pick_ad(conn, slug: str | None):
        if slug:
            return ad_by_slug(conn, slug)
        # Weighted random. Done in Python on purpose: SQLite's math functions
        # are a compile-time option, so an ORDER BY LOG(...) would work on one
        # machine and fail on the next.
        rows = conn.execute(
            "SELECT * FROM ads WHERE status='ready' AND active=1"
        ).fetchall()
        if not rows:
            return None
        weights = [max(1, r["weight"]) for r in rows]
        return random.choices(rows, weights=weights, k=1)[0]

    def render_snippet(request: Request, ad, max_width: int = 0,
                       align: str = "center", radius: int = 0) -> str:
        tpl = templates.env.get_template("embed.js")
        return tpl.render(
            base=config.PUBLIC_URL,
            slug=ad["slug"],
            kind=ad["kind"],
            width=ad["width"] or 0,
            height=ad["height"] or 0,
            max_width=max_width,
            align=align if align in ("left", "center", "right") else "center",
            radius=max(0, min(64, radius)),
            has_target=bool(ad["target_url"]),
            title=(ad["title"] or "").replace("\\", "\\\\").replace('"', '\\"'),
        )

    def ad_json(ad) -> dict:
        """One ad as data -- for building your own integration."""
        return {
            "slug": ad["slug"],
            "title": ad["title"],
            "kind": ad["kind"],
            "width": ad["width"],
            "height": ad["height"],
            "aspect_ratio": (round(ad["width"] / ad["height"], 4)
                             if ad["width"] and ad["height"] else None),
            "media_url": f"{config.PUBLIC_URL}/m/{ad['slug']}",
            "thumb_url": f"{config.PUBLIC_URL}/m/{ad['slug']}?thumb=1",
            "click_url": f"{config.PUBLIC_URL}/c/{ad['slug']}",
            "pixel_url": f"{config.PUBLIC_URL}/img/{ad['slug']}",
            "frame_url": f"{config.PUBLIC_URL}/frame/{ad['slug']}",
            "script_url": f"{config.PUBLIC_URL}/embed/{ad['slug']}.js",
            "has_target": bool(ad["target_url"]),
        }

    @app.get("/embed/random.js")
    def embed_random(request: Request, width: int = 0, align: str = "center",
                     radius: int = 0):
        with db.session() as conn:
            ad = pick_ad(conn, None)
        if ad is None:
            return Response("/* maxads: no active ad */", media_type="application/javascript",
                            headers={"Access-Control-Allow-Origin": "*"})
        return Response(render_snippet(request, ad, width, align, radius),
                        media_type="application/javascript",
                        headers={"Cache-Control": "no-store",
                                 "Access-Control-Allow-Origin": "*"})

    @app.get("/embed/{slug}.js")
    def embed_one(request: Request, slug: str, width: int = 0,
                  align: str = "center", radius: int = 0):
        with db.session() as conn:
            ad = pick_ad(conn, slug)
        if ad is None:
            return Response("/* maxads: unknown or inactive ad */",
                            media_type="application/javascript",
                            headers={"Access-Control-Allow-Origin": "*"})
        return Response(render_snippet(request, ad, width, align, radius),
                        media_type="application/javascript",
                        headers={"Cache-Control": "public, max-age=300",
                                 "Access-Control-Allow-Origin": "*"})

    # ---- embedding without JavaScript and without an iframe ------------------

    @app.get("/img/{slug}")
    def image_pixel(request: Request, slug: str):
        """Plain <img src> embedding: counts the impression on delivery.

        The simplest possible integration -- works in newsletters, forums and
        anywhere JavaScript is stripped. `random` picks a rotating ad.
        """
        with db.session() as conn:
            ad = pick_ad(conn, None if slug == "random" else slug)
            if ad is None:
                raise HTTPException(404)
            record_event(request, conn, ad["id"], "impression")
            path = Path(ad["media_path"])
            kind = ad["kind"]
            thumb = Path(ad["thumb_path"]) if ad["thumb_path"] else None
        # A video cannot be shown in an <img>; its thumbnail can.
        served = path if kind == "image" else (thumb or path)
        if not served.exists():
            raise HTTPException(404)
        return FileResponse(served, media_type="image/webp", headers={
            # no-store: an <img> that is cached would never count again.
            "Cache-Control": "no-store, max-age=0",
            "Access-Control-Allow-Origin": "*",
        })

    # ---- JSON: for your own integrations -------------------------------------

    @app.get("/api/ad/{slug}")
    def api_ad(slug: str):
        with db.session() as conn:
            ad = pick_ad(conn, None if slug == "random" else slug)
        if ad is None:
            raise HTTPException(404, "unknown or inactive ad")
        return JSONResponse(ad_json(ad), headers={
            "Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"})

    @app.get("/api/ads")
    def api_ads():
        """Every ad currently in rotation."""
        with db.session() as conn:
            rows = conn.execute(
                "SELECT * FROM ads WHERE status='ready' AND active=1 ORDER BY id"
            ).fetchall()
        return JSONResponse(
            {"count": len(rows), "ads": [ad_json(r) for r in rows]},
            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"})

    @app.get("/api", response_class=HTMLResponse)
    def api_docs(request: Request):
        with db.session() as conn:
            row = conn.execute(
                "SELECT slug FROM ads WHERE status='ready' AND active=1 ORDER BY id LIMIT 1"
            ).fetchone()
        return templates.TemplateResponse(request, "api.html", {
            "base": config.PUBLIC_URL,
            "example": row["slug"] if row else "abc123def456",
        })

    @app.get("/frame/{slug}", response_class=HTMLResponse)
    def frame(request: Request, slug: str):
        with db.session() as conn:
            ad = pick_ad(conn, None if slug == "random" else slug)
        if ad is None:
            return HTMLResponse("<!doctype html><title>maxads</title>", status_code=404)
        return templates.TemplateResponse(request, "frame.html", {
            "ad": dict(ad), "base": config.PUBLIC_URL,
        }, headers={"Access-Control-Allow-Origin": "*"})
