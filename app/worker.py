"""Media worker: turns uploads into web-ready assets.

Runs as its own systemd service. Transcoding a video takes minutes -- doing it
inside a request would block the web process and lose the job on every deploy
restart. A job table plus a separate process survives both.
"""
import errno
import logging
import os
import select
import signal
import sys
import time
from pathlib import Path

from . import config, db, media

log = logging.getLogger("maxads.worker")

# The worker used to sleep 2 s and look -- 43 200 wake-ups a day, almost always
# to find nothing. Measured, that cost no noticeable CPU time, but every timer
# keeps the processor out of its deep sleep states. Now it blocks on a pipe and
# is woken when something was actually uploaded. The timeout is only a safety
# net for the case that a wake-up gets lost.
IDLE_TIMEOUT_SECONDS = 300
MAX_ATTEMPTS = 3

_running = True
# The write end of the wake-up pipe, so that a signal can wake select().
_wake_fd: int | None = None


def _stop(signum, frame):  # noqa: ARG001
    """Ask the loop to finish.

    Setting the flag alone is NOT enough: since PEP 475 Python restarts an
    interrupted select() automatically once the handler returns, so a waiting
    worker would sleep on for the rest of its timeout and systemd would kill it
    after 90 s. It also drags the web service down with it -- systemd restarts
    both units as one transaction. So the handler writes a byte into the very
    pipe the loop is waiting on.
    """
    global _running
    _running = False
    if _wake_fd is not None:
        try:
            os.write(_wake_fd, b"x")   # async-signal-safe
        except OSError:
            pass
    log.info("shutdown requested, finishing current job")


def open_wakeup() -> int | None:
    """The read end of the wake-up pipe, non-blocking.

    Lives in /run (tmpfs, RAM) on purpose: a FIFO on the data disk would put
    the disk back into the idle path we are trying to keep quiet.
    """
    path = config.WAKEUP_FIFO
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            os.mkfifo(path, 0o600)
        # O_RDWR keeps the pipe open even with no writer -- opening read-only
        # non-blocking would hand back EOF forever once a writer disconnects.
        return os.open(path, os.O_RDWR | os.O_NONBLOCK)
    except OSError as exc:
        log.warning("no wake-up pipe (%s) -- falling back to the timeout only", exc)
        return None


def wait_for_work(fd: int | None, timeout: float) -> str:
    """Block until someone signals work, or the timeout expires."""
    if fd is None:
        time.sleep(timeout)
        return "timeout"
    try:
        ready, _, _ = select.select([fd], [], [], timeout)
    except InterruptedError:
        return "signal"
    if not ready:
        return "timeout"
    try:
        os.read(fd, 4096)   # drain -- several uploads are still one wake-up
    except OSError as exc:
        if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
            raise
    return "wakeup"


def claim_job(conn) -> dict | None:
    """Atomically take the oldest pending job.

    The UPDATE ... WHERE state='pending' is the lock: a second worker updating
    the same row changes zero rows and moves on.
    """
    row = conn.execute(
        "SELECT id, ad_id, attempts FROM jobs WHERE state = 'pending' "
        "ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    cur = conn.execute(
        "UPDATE jobs SET state='running', attempts=attempts+1, "
        "updated_at=datetime('now') WHERE id=? AND state='pending'",
        (row["id"],),
    )
    if cur.rowcount == 0:
        return None
    return {"id": row["id"], "ad_id": row["ad_id"], "attempts": row["attempts"] + 1}


def process(conn, job: dict) -> None:
    ad = conn.execute("SELECT * FROM ads WHERE id = ?", (job["ad_id"],)).fetchone()
    if ad is None:
        conn.execute("UPDATE jobs SET state='failed', last_error='ad gone' WHERE id=?",
                     (job["id"],))
        return

    src = Path(ad["raw_path"])
    if not src.exists():
        raise media.MediaError(f"original missing: {src}")

    log.info("processing ad %s (%s, %s)", ad["slug"], ad["kind"], src.name)
    started = time.monotonic()

    if ad["kind"] == "image":
        result = media.process_image(src, ad["slug"])
    else:
        result = media.process_video(src, ad["slug"])

    conn.execute(
        "UPDATE ads SET status='ready', media_path=?, thumb_path=?, width=?, "
        "height=?, error=NULL WHERE id=?",
        (str(result.media_path), str(result.thumb_path),
         result.width, result.height, ad["id"]),
    )
    conn.execute(
        "UPDATE jobs SET state='done', last_error=NULL, updated_at=datetime('now') "
        "WHERE id=?", (job["id"],),
    )
    log.info("ad %s ready in %.1fs (%dx%d)", ad["slug"],
             time.monotonic() - started, result.width, result.height)


def handle_failure(conn, job: dict, exc: Exception) -> None:
    message = str(exc)[:800]
    if job["attempts"] >= MAX_ATTEMPTS:
        conn.execute("UPDATE jobs SET state='failed', last_error=?, "
                     "updated_at=datetime('now') WHERE id=?", (message, job["id"]))
        conn.execute("UPDATE ads SET status='failed', error=? WHERE id=?",
                     (message, job["ad_id"]))
        log.error("ad %s failed permanently: %s", job["ad_id"], message)
    else:
        # Back to pending for another attempt on the next poll.
        conn.execute("UPDATE jobs SET state='pending', last_error=?, "
                     "updated_at=datetime('now') WHERE id=?", (message, job["id"]))
        log.warning("ad %s attempt %d failed: %s", job["ad_id"], job["attempts"], message)


def requeue_stale(conn) -> int:
    """Jobs left in 'running' by a killed worker would never be picked up again."""
    cur = conn.execute(
        "UPDATE jobs SET state='pending' WHERE state='running' "
        "AND updated_at < datetime('now', '-1 hour')"
    )
    return cur.rowcount


def main() -> int:
    config.require_mount()
    config.ensure_dirs()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            config.file_handler("worker.log"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    tools = media.tools_available()
    if not all(tools.values()):
        log.error("missing tools: %s", [k for k, v in tools.items() if not v])
        return 1

    db.migrate()
    with db.session() as conn:
        n = requeue_stale(conn)
        if n:
            log.info("requeued %d stale job(s)", n)

        fd = open_wakeup()
        global _wake_fd
        _wake_fd = fd
        log.info("worker started (event driven, fallback every %ds)", IDLE_TIMEOUT_SECONDS)
        while _running:
            job = claim_job(conn)
            if job is not None:
                try:
                    process(conn, job)
                except Exception as exc:  # noqa: BLE001 - a bad upload must not kill the worker
                    handle_failure(conn, job, exc)
                # Straight on to the next one: a backlog must not wait.
                continue
            wait_for_work(fd, IDLE_TIMEOUT_SECONDS)
        _wake_fd = None
        if fd is not None:
            os.close(fd)
    log.info("worker stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
