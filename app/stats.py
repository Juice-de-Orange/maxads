"""Storage figures and delivery metrics for the dashboard."""
import ipaddress
import shutil
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

from . import config


def _dir_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


def human(n: float) -> str:
    """Bytes as a short human string: 1.5 kB, 5.0 GB."""
    for unit, factor in (("TB", 1 << 40), ("GB", 1 << 30), ("MB", 1 << 20), ("kB", 1 << 10)):
        if n >= factor:
            value = n / factor
            text = f"{value:.1f}" if value < 100 else f"{value:.0f}"
            return f"{text} {unit}"
    return f"{int(n)} B"


def storage() -> dict:
    """How full is the data disk, and what is filling it?

    `used` comes from the filesystem, not from summing our folders: the
    difference between the two is exactly the data we do NOT control, and
    hiding it would make a full disk look like our own fault when it isn't.
    """
    usage = shutil.disk_usage(config.BASE_DIR)
    raw = _dir_size(config.RAW_DIR)
    media = _dir_size(config.MEDIA_DIR)
    db = _dir_size(config.DB_DIR)
    logs = _dir_size(config.LOG_DIR)
    ours = raw + media + db + logs
    other = max(0, usage.used - ours)

    def pct(n: int) -> float:
        return round(n / usage.total * 100, 2) if usage.total else 0.0

    return {
        "total": usage.total, "used": usage.used, "free": usage.free,
        "total_h": human(usage.total), "used_h": human(usage.used), "free_h": human(usage.free),
        "used_pct": pct(usage.used), "free_pct": pct(usage.free),
        "segments": [
            {"key": "raw",   "label": "Originals",    "bytes": raw,   "h": human(raw),   "pct": pct(raw)},
            {"key": "media", "label": "Delivered",    "bytes": media, "h": human(media), "pct": pct(media)},
            {"key": "other", "label": "Other",        "bytes": db + logs + other,
             "h": human(db + logs + other), "pct": pct(db + logs + other)},
        ],
        "db_h": human(db), "logs_h": human(logs),
    }


def _is_local(host: str) -> bool:
    """Loopback, link-local or a private network -- someone's test page."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host in ("localhost",) or host.endswith(".local")
    return ip.is_loopback or ip.is_private or ip.is_link_local


def own_host() -> str:
    return (urlparse(config.PUBLIC_URL).hostname or "").lower()


def domain_of(referer: str) -> str | None:
    """Host part of a referer, without port or 'www.'.

    Returns None for everything that is not a foreign, public page:

    - our own host, because the iframe route sets a referer pointing back
      here -- counting it would make the service look like its own customer,
    - loopback and private addresses, because those are test pages,
    - anything unparseable, so a junk referer never becomes a counted site.
    """
    if not referer:
        return None
    try:
        host = (urlparse(referer).hostname or "").lower()
    except ValueError:
        return None
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]
    if _is_local(host):
        return None
    if host == own_host():
        return None
    # A bare hostname without a dot is not a public site either.
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if "." not in host:
            return None
    return host


def record_site(conn: sqlite3.Connection, ad_id: int, referer: str, clicked: bool = False) -> None:
    """Count one delivery to a foreign page.

    Runs on every request, not only on counted impressions -- otherwise a page
    a returning visitor sees would never appear in the site list.
    """
    domain = domain_of(referer)
    if domain is None:
        return
    conn.execute(
        "INSERT INTO sites(ad_id, domain, hits, clicks) VALUES (?, ?, 1, ?) "
        "ON CONFLICT(ad_id, domain) DO UPDATE SET "
        "  hits = hits + 1, clicks = clicks + ?, last_seen = datetime('now')",
        (ad_id, domain, int(clicked), int(clicked)),
    )


def metrics(conn: sqlite3.Connection) -> dict:
    """Totals for the dashboard header."""
    row = conn.execute(
        "SELECT COALESCE(SUM(kind='impression'), 0) AS imp, "
        "       COALESCE(SUM(kind='click'), 0) AS clk FROM events"
    ).fetchone()
    impressions, clicks = row["imp"], row["clk"]

    sites_total = conn.execute(
        "SELECT COUNT(DISTINCT domain) AS n FROM sites"
    ).fetchone()["n"]
    ads_total = conn.execute("SELECT COUNT(*) AS n FROM ads").fetchone()["n"]
    ads_active = conn.execute(
        "SELECT COUNT(*) AS n FROM ads WHERE active = 1 AND status = 'ready'"
    ).fetchone()["n"]

    top_sites = [dict(r) for r in conn.execute(
        "SELECT domain, SUM(hits) AS hits, SUM(clicks) AS clicks "
        "FROM sites GROUP BY domain ORDER BY hits DESC LIMIT 8"
    )]

    return {
        "impressions": impressions,
        "clicks": clicks,
        "ctr": round(clicks / impressions * 100, 1) if impressions else 0.0,
        "sites": sites_total,
        "ads_total": ads_total,
        "ads_active": ads_active,
        "top_sites": top_sites,
    }


def sites_for(conn: sqlite3.Connection, ad_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT domain, hits, clicks, last_seen FROM sites WHERE ad_id = ? "
        "ORDER BY hits DESC LIMIT 10", (ad_id,)
    )]
