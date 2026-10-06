# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.1.0] - 2026-10-06

### Added

- First public release: banner upload with WebP/H.264 optimisation in a separate
  worker, five embedding routes (script, script with options, iframe, plain
  image, JSON), per-day deduplicated impressions and clicks without stored IPs,
  site metrics, storage overview, Docker and systemd setups.
- The first admin password can be set with `MAXADS_ADMIN_PASSWORD`; a generated
  one is shown once on stderr, never logged, and must be changed on first login.
- Mount guard can be switched off for container volumes (`MAXADS_REQUIRE_MOUNT=0`).

### Changed

- English user interface; environment variables are prefixed `MAXADS_`.
- The host port of the Docker quick start is configurable with `MAXADS_PORT`.

### Fixed

- A failed dashboard login no longer writes the client IP address to the log.
- A banner title can no longer inject script into the delete confirmation; an
  apostrophe in the title used to skip the confirmation (#10).
- A click on a banner without a target URL is no longer counted; a failed or
  converting banner no longer shows "on air" (#10).
- Dashboard form errors (file type, size, password length, duplicate user) show
  on the dashboard instead of as raw JSON (#12).
