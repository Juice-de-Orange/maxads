# Running MaxAds with systemd

The Docker setup in the README is the quick path. This page is for a plain Linux
host (or an LXC/VM) with the two hardened units in this folder. Paths below are
the ones the templates use — change them consistently if yours differ.

| Path | What |
|---|---|
| `/opt/maxads` | the code (a checkout of this repository) and `venv/` |
| `/srv/maxads` | `MAXADS_BASE`: originals, media, database, logs — ideally its own disk |
| `/etc/maxads.env` | environment, see [`.env.example`](../../.env.example) |
| `/run/maxads` | created by systemd: the wake-up pipe between web and worker |

## Install

```bash
sudo apt-get install -y python3 python3-venv ffmpeg
sudo useradd --system --home-dir /opt/maxads --shell /usr/sbin/nologin maxads

sudo git clone https://github.com/Juice-de-Orange/maxads.git /opt/maxads
sudo python3 -m venv /opt/maxads/venv
sudo /opt/maxads/venv/bin/pip install -r /opt/maxads/requirements.txt

# The data directory. On a separate disk: mount it first, then create the
# sentinel ON the mounted disk -- the service refuses to start without it.
sudo mkdir -p /srv/maxads
sudo touch /srv/maxads/.mounted
sudo chown -R maxads:maxads /opt/maxads /srv/maxads

sudo install -m 600 /opt/maxads/.env.example /etc/maxads.env   # then edit it
sudo install -m 644 /opt/maxads/docs/deploy/maxads.service /opt/maxads/docs/deploy/maxads-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now maxads.service maxads-worker.service
```

The generated first-admin password is in the journal once:
`journalctl -u maxads | grep "First admin"`. Set `MAXADS_ADMIN_PASSWORD` in
`/etc/maxads.env` before the first start if you prefer to choose it.

## Reverse proxy

The web unit listens on `127.0.0.1:8080` and trusts `X-Forwarded-*` headers only
from `127.0.0.1`. If your proxy runs on another machine, change `--host` and
`--forwarded-allow-ips` in `maxads.service` to match. Example for Caddy:

```
ads.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

Set `MAXADS_PUBLIC_URL=https://ads.example.com` in `/etc/maxads.env`.

## Updating

```bash
cd /opt/maxads && sudo -u maxads git pull
sudo -u maxads /opt/maxads/venv/bin/pip install -r requirements.txt
# One at a time: `systemctl restart A B` is ONE transaction, and a worker that
# finishes a running transcode would keep the web service down meanwhile.
sudo systemctl restart maxads.service
sudo systemctl restart maxads-worker.service
curl -fsS http://127.0.0.1:8080/healthz
```

## When something is stuck

```bash
systemctl status maxads maxads-worker
journalctl -u maxads-worker -n 50
curl -s http://127.0.0.1:8080/healthz
```

**`/healthz` reports `data-not-mounted`:** the data disk is gone. The service
deliberately does **not** start in that state — it would otherwise write into an
empty directory and lose the uploads. Mount the disk, then restart both units.

**`missing-ffmpeg` / `missing-ffprobe`:** install ffmpeg; the worker refuses to
run without it.

## What the units restrict

Both units run as `maxads`, with `NoNewPrivileges`, `ProtectSystem=strict`,
`ProtectHome`, `PrivateTmp` and write access only to the data directory. Memory
and task limits sit well above normal use (the web process needs about 40 MB);
the worker runs with `Nice=10`, idle I/O priority and a low CPU weight so that
transcoding never starves the web service.
