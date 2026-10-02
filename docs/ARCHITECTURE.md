# How MaxAds is built

This file explains the **decisions**. What the service does and how to run it is
in the [README](../README.md); running it without Docker is in
[deploy/systemd.md](deploy/systemd.md).

---

## The path of a request

```
Browser on some foreign page
   │  <script src="https://ads.example.com/embed/<id>.js">
   ▼
Reverse proxy (TLS ends here)
   ▼
web — uvicorn, port 8080
   │
   ├── reads media from   $MAXADS_BASE/data/media
   └── writes events to   $MAXADS_BASE/data/db/maxads.db
```

`MAXADS_PUBLIC_URL` must be the address the foreign page reaches, because every
snippet, iframe and JSON response builds its URLs from it.

## Why the media worker is a separate process

Converting a video takes minutes. Inside a request (or a `BackgroundTask`) it would

- block the web server while it runs, and
- get lost on every restart, because it only exists in memory.

A job table in SQLite plus a second process survives both. The worker claims a
job with `UPDATE … WHERE state='pending'` — that *is* the lock: a second worker
changes zero rows and moves on. Jobs left in `running` by a killed worker are
put back after an hour.

It does **not** poll. It blocks on a FIFO under `/run/maxads` (tmpfs) and is woken
by the upload; a 300 s timeout is only the safety net. A failed wake-up must never
fail an upload — the job is in the database and the timeout finds it at the latest.

Stopping needs care: since PEP 475 Python restarts an interrupted `select()` by
itself, so a signal handler that only sets a flag leaves the worker asleep until
its timeout — and systemd kills it after 90 s. The handler therefore writes a byte
into the very pipe the loop waits on. A test sends a real `SIGTERM` to prove it.

## Why sites live in a table of their own

The obvious way to answer "on how many sites does the ad run" would be to count
domains in `events`. That would be wrong: impressions are **deduplicated per
visitor, banner and day** (a unique index makes sure a reload does not inflate
the number). Counting domains from the same row would miss every second page a
returning visitor sees.

`record_site()` therefore runs **outside** the `INSERT OR IGNORE`, on every
request. A test pins exactly that.

Not counted: our own host (the iframe route sets a referer pointing back at us —
counted, the service would look like its own customer), loopback, private
networks and `*.local`.

## Why no raw IP addresses are stored

Telling visitors apart needs no more than a hash of IP, user agent, **the date**
and a secret. The next day the same visitor cannot be recognised. The numbers
stay useful without the service becoming a visitor log.

## Why browser compatibility beats file size

Video processing never delivers more bytes than were uploaded — **unless** the
original is not browser-ready. An H.264 file in `yuv444p` ("High 4:4:4
Predictive") plays fine in VLC and hangs forever in Chrome: `readyState` stays 0
and **no** error event fires.

ffmpeg's own `testsrc` produces exactly that, and it compresses so well that the
transcoded version comes out larger. A plain size comparison would have passed the
unplayable original straight through. So codec, pixel format, profile and width
are checked.

## Why the service refuses to start when the data disk is missing

A data disk in `/etc/fstab` usually carries `nofail` — without it the host hangs
at boot when the disk is gone. The price: when the disk drops out, everything
keeps running, and a service without a guard would silently write its uploads
into an empty directory on the system disk.

So the data directory holds a sentinel file `.mounted`; `ExecStartPre` checks it,
and `/healthz` reports `data-not-mounted` if it disappears while running. The
Docker image switches the guard off (`MAXADS_REQUIRE_MOUNT=0`), because a named
volume cannot drop out on its own.

## Why the first admin password never reaches the log

There is no public registration, so the first account has to come from
somewhere. With `MAXADS_ADMIN_PASSWORD` the operator chooses it. Without, a random
password is generated, printed **once to stderr** and the account is locked to
the password form until it is changed. It never goes through `logging`: the log
files live on the data disk and would keep the password long after the first
login.

## Deploying from CI without handing out root

The original deployment rolled out every push to `main` through a self-hosted CI
runner. The pattern is worth copying if you do the same:

- The runner may run **exactly one** command as root via `sudo`: an installed
  copy of the deploy script that no push can reach. If a push could change the
  script it runs as root, everyone with push access would be root.
- **systemd units are not installed from the repository.** Otherwise a unit with
  `User=root` in a pull request would be root access by the back door.
- **`pip install` runs as the service user**, not as root. A poisoned package in
  `requirements.txt` gets at most the rights of the service.
- What remains is unavoidable: whoever can push runs code as the service user —
  that *is* the application.

This repository ships no deploy workflow; never register a self-hosted runner on
a public repository, because pull requests from forks could run on it.

---

## Database

SQLite in WAL mode, one connection per request. Migrations run at start-up, one
by one, each in a transaction.

> `executescript()` would be the obvious way and is the wrong one: it commits the
> open transaction implicitly, which turns the surrounding `BEGIN`/`COMMIT` into a
> no-op and makes a half-applied migration impossible to roll back. Hence
> `split_statements()`.

| Table | Purpose |
|---|---|
| `users` | login; every user has full rights |
| `ads` | the banners, with `status` (`processing` / `ready` / `failed`) |
| `jobs` | queue for the worker |
| `events` | impressions and clicks, deduplicated per visitor and day |
| `sites` | foreign pages, counted on **every** request |

---

## What is deliberately missing

- **No built-in backup.** Back up the data directory (`$MAXADS_BASE`) with the
  tool you already use; the SQLite file is safe to copy with `sqlite3 .backup`.
- **No public registration.** The first admin is created at start-up, further
  accounts only from the dashboard.
- **No roles.** Every account can do everything — it is a tool for a small team.
- **No user deletion** in the interface. On the database it works; `ads.created_by`
  has to be reassigned first, or the foreign key blocks it.
