# Release Process

Caura (formerly MemClaw) follows [Semantic Versioning](https://semver.org/) and uses <!-- legacy-name-floor: taught as legacy alias -->
[release-please](https://github.com/googleapis/release-please) to manage
releases automatically from [Conventional Commits](https://www.conventionalcommits.org/).

The repo ships **three independently-versioned release-please components**:

| Package | Path     | Tag format       | Baseline |
| ------- | -------- | ---------------- | -------- |
| backend | `.`      | `backend-vX.Y.Z` | 2.5.0    |
| plugin  | `plugin/`| `plugin-vX.Y.Z`  | 2.5.0    |
| collaboration-clients | `clients/collaboration/` | `collaboration-clients-vX.Y.Z` | 0.2.0 (unpublished preview; first release `0.3.0`) |

Backend and plugin release on independent cadences. Plugin fixes no longer
require a backend release; backend changes don't force a plugin version
bump unless the plugin source itself changes.

## How it works

1. Push commits to `main` using Conventional Commit messages (`feat:`,
   `fix:`, `perf:`, `docs:`, `refactor:`, `chore:` etc.).
2. release-please reads `release-please-config.json` +
   `.release-please-manifest.json` and routes each commit to one or both
   packages **by path**: commits touching files under `plugin/**` bump the
   plugin package; everything else bumps the backend package.
3. release-please opens a single combined Release PR
   (`separate-pull-requests: false`) containing per-component sections in
   `CHANGELOG.md` and updated version files for whichever packages had
   changes.
4. Merging the Release PR tags each updated package (e.g. `plugin-v2.5.1`)
   and triggers the corresponding release workflows.

## Version files release-please rewrites

**Backend (`.`):**
- `core-api/pyproject.toml` (`$.project.version`)
- `core-worker/pyproject.toml` (`$.project.version`)
- `core-storage-api/pyproject.toml` (`$.project.version`)

**Plugin (`plugin/`):**
- `plugin/package.json` (`$.version`) — handled by `release-type: node`
- `plugin/openclaw.plugin.json` (`$.version`)
- `plugin/src/version.ts` — `generic` updater, keyed on the
  `x-release-please-version` annotation that `scripts/gen-version.sh`
  emits. Locally the file is still generated only by that script
  (build/test hooks); the extra-files entry exists so release PRs
  don't carry a stale version and trip CI's `check:version` gate.

**Collaboration clients (`clients/collaboration/`):**
- `{core,mcp,cli,adapter-sdk}/pyproject.toml` — `generic` updater on the
  lines marked `x-release-please-version`: each package's own `version` and
  the dependents' exact `caura-bus-core==X.Y.Z` pin.
- `clients/collaboration/uv.lock` (`$.package[...].version` for each member).
- `clients/collaboration/README.md` — the install snippet inside the
  `x-release-please-start-version` block.

## Collaboration client distributions

`clients/collaboration/` releases four PyPI distributions in **lockstep**, one
version for all of them:

| Distribution | Console entrypoint | Depends on |
| ------------ | ------------------ | ---------- |
| `caura-bus-core` | none (library) | — |
| `caura-bus-mcp` | `caura-bus-mcp` | `caura-bus-core==<same version>` |
| `caura-bus-cli` | `caura-bus` | `caura-bus-core==<same version>` |
| `caura-bus-adapter-sdk` | `caura-bus-adapter-echo` | `caura-bus-core==<same version>` |

The exact pin is deliberate: the wire models live in `caura-bus-core`, and a
`caura-bus-mcp` resolved against a different core could silently drop or
misread fields. Upgrade the four together.

Release flow:

1. Conventional commits under `clients/collaboration/**` route to this
   component and accumulate in the release-please PR.
2. Merging that PR (review required) creates the GitHub release
   `collaboration-clients-vX.Y.Z`.
3. The release triggers `publish-python-collaboration-clients.yml`. Its build
   job re-runs the client tests, builds wheels and sdists, and runs
   `clients/collaboration/scripts/verify_dist.py`, which installs the four
   wheels into a fresh virtualenv with no source tree in reach and runs every
   entrypoint. Its publish job waits for approval on the
   `pypi-collaboration` environment, then uploads with PyPI trusted
   publishing and PEP 740 attestations (core first, then its dependents).

`release-as: "0.3.0"` in `release-please-config.json` fixes the first published
version. `0.2.0` was only ever the version string of unpublished sibling-path
preview builds, so publishing starts at `0.3.0` to keep a preview install and a
published artifact distinguishable. **Remove `release-as` in the first commit
after `collaboration-clients-v0.3.0` is released**, or every later release
PR will keep proposing `0.3.0`.

Check the build locally before cutting a release:

```sh
cd clients/collaboration
uv build --all-packages
python3 -I scripts/verify_dist.py dist
```

## Commit scope conventions

To make changelogs scannable, prefix the conventional-commit description
with a scope when it helps:

- `feat(plugin): add deploy command retry`
- `fix(core-api): handle null tenant_id in heartbeat`
- `fix(plugin,core-api): align deploy payload schema`

The scope is cosmetic — package routing is purely path-based. A commit
that touches both `plugin/` and `core-api/` will bump **both** packages
with the same conventional-commit type. Split such commits when the
change is logically separable; bundle when the cross-cut is intentional
(e.g. a new API endpoint plus the plugin client that calls it).

## Compatibility

There is no hard handshake. Backend logs a warning when a heartbeat
reports a plugin version below `MIN_RECOMMENDED_PLUGIN_VERSION`
(`core-api/src/core_api/version_compat.py`). Bump that constant when a
backend change requires a newer plugin.

The plugin's install endpoint (`/api/v1/install-plugin`) stamps the
installed plugin with **plugin's** version (`plugin/package.json`), not
backend's `VERSION`. Both are reported in heartbeat payloads
(`plugin_version`, `openclaw_version`).

## API compatibility (REST request bodies)

Separate from the plugin/backend question above: the REST surface has a
request-body contract that integrators depend on, and it is asymmetric on
purpose.

- **Write and mutation bodies are strict.** A field a request model does not
  declare returns `422` naming it (`error.details.unknown_fields`). Adding a
  new OPTIONAL field to a write model is additive and safe. REMOVING or
  RENAMING one is breaking twice over: the old spelling stops being accepted
  *and* stops being ignored.
- **Search / filter / query bodies are permissive** and stay that way. They
  absorb historical spellings via `AliasChoices`.

The full contract, including the two write bodies deliberately left permissive
(bulk `items[]` and the plugin telemetry endpoints), is in
[`docs/api-surfaces.md`](docs/api-surfaces.md#request-body-contract-writes-are-strict-searches-are-not).

Announce a change to this contract the way every other breaking change is
announced — a `BREAKING CHANGE:` footer on the conventional commit, which
release-please renders as a "⚠ BREAKING CHANGES" section in `CHANGELOG.md` and
bumps the major. Do not hand-edit `CHANGELOG.md`: release-please regenerates
it from merged PR titles, and the legacy-name ratchet only exempts that file on
release branches.

## Manual emergency release

If release-please is unavailable, bump the affected package's version
files manually, update `CHANGELOG.md`, and tag with the component-
namespaced format (`backend-vX.Y.Z`, `plugin-vX.Y.Z` or
`collaboration-clients-vX.Y.Z`), then publish the release so the
corresponding publish workflow runs.
