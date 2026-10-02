# Security Policy

## Supported versions

MaxAds is developed on `main`; fixes land there. Please update to the latest
commit before reporting.

## Reporting a vulnerability

Please **do not** open a public issue, discussion or pull request for security
problems.

Report privately through GitHub's private vulnerability reporting on this
repository: **Security → Report a vulnerability**. Include a description, the
commit you tested and steps to reproduce.

You will receive an acknowledgement within **7 days**. A fix or workaround is
aimed for within **90 days** of triage, followed by a GitHub security advisory.

## Scope

In scope: authentication and sessions, the upload path and media processing
(Pillow, ffmpeg invocation), the public delivery and counting endpoints, the
container image and the systemd units.

Worth knowing up front, and not a vulnerability by itself:

- Every account has full rights by design; MaxAds is a tool for a small team.
- The public endpoints are open to any origin (`Access-Control-Allow-Origin: *`)
  on purpose — banners are embedded on foreign pages.
- Run it behind a reverse proxy with TLS. Over plain HTTP the session cookie
  travels in clear text.
