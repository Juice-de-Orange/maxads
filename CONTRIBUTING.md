# Contributing to MaxAds

Thanks for taking the time to contribute! Bug reports, ideas and pull requests
are welcome.

Good places to start: issues labelled
[`good first issue`](https://github.com/Juice-de-Orange/maxads/labels/good%20first%20issue)
and [`help wanted`](https://github.com/Juice-de-Orange/maxads/labels/help%20wanted).
Larger changes are best discussed in an issue first.

## Before you start

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). It explains the decisions and
the traps they avoid — the separate `sites` table, the FIFO wake-up and its
signal handling, the browser-compatibility check for videos. A change that breaks
one of them usually still passes a naive test.

## Development setup

```bash
python3.13 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q
python -m ruff check .
```

Install `ffmpeg` locally: without it the two video tests skip themselves, and CI
fails on any skipped test.

### Secret guard

The repository ships a [pre-commit](https://pre-commit.com/) hook that runs
[gitleaks](https://github.com/gitleaks/gitleaks) on every commit:

```bash
pip install pre-commit
pre-commit install
```

CI runs the same scanner over the full history. Never put real credentials,
hostnames, IP addresses or personal data into any file, test or screenshot — use
`.env` (see `.env.example`) and documentation placeholders (`example.com`,
`192.0.2.x`).

## Branch and commit conventions

- Fork, then branch from `main`; name the branch `<kind>/<short-slug>`
  (e.g. `feat/webm-output`, `fix/gif-loop`).
- Commits follow [Conventional Commits 1.0.0](https://www.conventionalcommits.org/):
  `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `ci:`, `chore:`.
- Sign off your commits with the
  [Developer Certificate of Origin](https://developercertificate.org/):
  `git commit -s`. There is no CLA.

## Pull requests

- A bug fix comes with a test that fails without it.
- UI changes: attach a screenshot made with synthetic banners.
- User-facing changes update `README.md`, `.env.example` or the docs.

## License

By contributing you agree that your contributions are licensed under the
[MIT License](LICENSE).
