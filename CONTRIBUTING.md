# Contributing to MemClaw

Thanks for your interest in contributing! This document covers how to get set
up, the expected workflow, and how we review changes.

## Ground Rules

- By contributing, you agree that your contributions will be licensed under the
  [Apache License 2.0](LICENSE).
- **Sign off every commit** under the [Developer Certificate of Origin](https://developercertificate.org/) (DCO) — see "Sign-off (DCO)" below.
- **Use [Conventional Commits](https://www.conventionalcommits.org/)** for commit subjects — see "Commit messages" below.
- Be respectful. See our [Code of Conduct](CODE_OF_CONDUCT.md).
- For security issues, see [SECURITY.md](SECURITY.md) — do not open a public
  issue.

## Development Setup

### Prerequisites

- Python 3.12+
- PostgreSQL 16+ with the `pgvector` extension (or use Docker Compose)
- Node.js 20+ (only if you're working on the `plugin/` directory)
- `uv` (recommended) or `pip` for Python dependency management

### Clone and set up a venv

```bash
git clone https://github.com/caura-ai/caura-memclaw.git
cd caura-memclaw
uv venv .venv
source .venv/bin/activate
uv pip install -e "core-api/[dev]" -e "core-storage-api/[dev]"
```

### Install the pre-commit hook

One-time setup that makes every `git commit` run `ruff check` and
`ruff format` — catches formatting regressions before they reach the PR:

```bash
pip install pre-commit   # or: uv pip install pre-commit
pre-commit install
```

The hook is configured in `.pre-commit-config.yaml` and only runs against
`core-api/src/` and `core-storage-api/src/`. `mypy` is intentionally not in
the hook (it needs the real project venv to resolve workspace imports
correctly) — CI runs it authoritatively, and you can run it locally via
the command under "Run local checks" below.

### Run services locally

The fastest path is Docker Compose:

```bash
docker compose -f docker-compose.dev.yml up -d
```

This starts PostgreSQL + pgvector, Redis, and the core API with hot reload.

### Run the test suite

```bash
pytest tests/ -v
```

See `README.md` for more deployment options and environment variable details.

## Workflow

1. **Open an issue first** for anything non-trivial — a bug fix under ~30 lines
   is fine to submit directly as a PR, but larger changes benefit from
   discussion before code is written.
2. **Create a branch** from `main` with a short descriptive name (e.g. `feat/fleet-id-filter`, `fix/plugin-heartbeat`).
3. **Make your change.** Keep PRs focused — one logical change per PR.
4. **Add or update tests.** We don't accept new features without tests, and bug
   fixes should include a regression test.
5. **Run local checks.** With the pre-commit hook installed, ruff check +
   ruff format already ran on `git commit`, so you only need:
   ```bash
   mypy core-api/src/ core-storage-api/src/
   pytest tests/
   ```
   Without the hook, also run ruff by hand:
   ```bash
   ruff check core-api/src/ core-storage-api/src/
   ruff format --check core-api/src/ core-storage-api/src/
   mypy core-api/src/ core-storage-api/src/
   pytest tests/
   ```
6. **Open a PR against `main`.** Fill out the PR template. Branch protection requires CI green, DCO check green, and ≥1 maintainer approval before merge.
7. **Respond to review.** Expect at least one round of feedback.

## Commit messages

We use [Conventional Commits](https://www.conventionalcommits.org/). The
subject line must be `<type>(<optional-scope>): <subject>`, with the
type drawn from this list:

| Type | Effect on the next release |
|---|---|
| `feat` | Minor bump |
| `fix` | Patch bump |
| `perf` | Patch bump |
| `deps` | Patch bump (auto-applied by Dependabot) |
| `revert` | Patch bump |
| `docs` | No release impact, surfaces in CHANGELOG |
| `refactor` | No release impact, surfaces in CHANGELOG |
| `test`, `build`, `ci`, `chore` | Hidden from CHANGELOG, no release impact |

Append `!` (`feat!: …`) or a `BREAKING CHANGE:` footer for changes
that break the [Public API](README.md#public-api--stability) — these
trigger a major bump once we ship 1.0.0. Before 1.0.0 they're treated
as minor (`bump-minor-pre-major: true` in `release-please-config.json`).

**Scopes** (optional but encouraged): `core-api`, `core-storage-api`,
`core-worker`, `plugin`, `common`, `mcp`, `skill`, `e2e`, `benchmarks`,
`docs`, `ci`.

**Other rules:**

- Subject line under 72 characters, imperative mood ("add X", not "added X").
- Include context in the body when the change is non-obvious.
- One logical change per commit.

**Common parser breakers — avoid in subject lines:**

- **Leading ticket identifier** (e.g. `CAURA-129 feat(contradiction): …`).
  The conventional-commit parser expects the subject to *start* with a
  type token; anything before `feat`/`fix`/etc. fails with
  `unexpected token ' '`. Put the ticket id in the **body**
  (`Closes CAURA-129.`) instead.
- **Unicode ellipsis `…` (U+2026)** anywhere in the subject. The
  parser rejects it with the same error class. If GitHub's squash-merge
  UI truncates a long title with `…`, rewrite the title before merging.
- **Non-ASCII punctuation generally** (em-dashes `—`, smart quotes
  `“ ”`). `ruff` will also flag these in code comments; the parser
  is even less forgiving in commit titles. Use plain `-` and `"`.

**Squash-merge note:** PR titles must themselves be Conventional Commits,
because the squash-merge commit on `main` is what release-please reads.
Reviewers will rename PR titles before merging if needed. A single
un-parseable subject line in the post-tag window can block the entire
next release from being opened — when in doubt, lean strict.

## Sign-off (DCO)

Every commit must carry a `Signed-off-by:` trailer asserting the
[Developer Certificate of Origin](https://developercertificate.org/) —
a one-paragraph statement that you have the right to submit the work
under the project's license. This is a lightweight alternative to a CLA
and is what the Linux kernel, Docker, and GitLab use.

Sign off automatically with `git commit -s`:

```bash
git commit -s -m "feat(plugin): handle MEMCLAW_API_PREFIX override"
```

Configure once and forget:

```bash
git config --global format.signoff true
```

A repo workflow checks every PR for sign-off. PRs without sign-off on
each commit will fail the DCO check and cannot be merged. If you forget,
the PR comment will tell you exactly which commits are missing it and
how to fix it (`git commit --amend -s` for the most recent, or `git rebase`
for older commits).

## Release process

Releases are cut automatically from `main` by
[release-please](https://github.com/googleapis/release-please-action).
On every push to `main` with new `feat:`, `fix:`, etc. commits,
release-please opens or updates a release PR. Merging the release PR:

1. Tags `vX.Y.Z` and creates a GitHub Release.
2. Updates `CHANGELOG.md` with everything since the last tag,
   grouped by Conventional Commit type.
3. Bumps the version in every pinned file: `VERSION`, every
   `pyproject.toml`, `plugin/package.json`, `plugin/openclaw.plugin.json`.

You don't bump versions by hand; just write good commit messages.

## Code Style

- Python: `ruff` for linting and formatting, `mypy` for type checking.
  Configuration lives in `core-api/pyproject.toml` and
  `core-storage-api/pyproject.toml`.
- TypeScript (`plugin/`): TypeScript strict mode, `tsc` for type checking.
- Line length: 110 characters for Python.
- No trailing whitespace, LF line endings.

## Questions

For questions that aren't bug reports, use
[GitHub Discussions](../../discussions) rather than opening an issue.
