# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

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
