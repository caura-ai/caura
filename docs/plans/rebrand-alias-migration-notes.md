# Rebrand alias migration notes

Compatibility policy is owned by the
[`caura-enterprise` compatibility register](https://github.com/caura-ai/caura-enterprise/blob/dev/COMPATIBILITY-REGISTER.md).
That register classifies each retained or removable surface from current evidence. This
document keeps only migration instructions and source evidence that are useful while the
remaining one-release bridges are removed; it does not assign permanent or sunset policy.

## Current user migration

- Use `caura-client` imports and `Caura*` class names. The yanked
  `memclaw-client==0.5.0` distribution installs `caura-client>=1.0.0`, but its wheel does <!-- legacy-name-floor: historical package evidence -->
  not provide a `memclaw_client` module. <!-- legacy-name-floor: historical module evidence -->
- Run `caura-interviewer install` after upgrading. It replaces an existing pre-rename
  crontab entry instead of writing a duplicate beside it.
- Use current Caura package, endpoint, and environment-variable names in new
  configuration. Existing compatibility behavior is row-specific; consult the register
  rather than assuming every retired name has the same lifetime.

## Active legacy writers

The Interviewer still writes its pre-rename configuration directory, managed cron marker,
and per-user lock filename. Those are active writers, not time-bounded cleanup shims. They
must remain until a separate change introduces canonical writers and migrates existing
state; a successor release by itself is not a removal gate.

## One-release cleanup bridges

Two true readers/removers intentionally survive for one `caura-client` release after their
old writer or entry point was removed:

- `caura_client.interviewer` checks the pre-rename executable so an upgrade can find and
  replace an existing installation.
- The generated plugin installer removes the pre-rename systemd TLS drop-in before writing
  the current one, preventing duplicate `NODE_EXTRA_CA_CERTS` declarations.

Remove these bridges only after release engineering confirms that the successor release
containing them has shipped. Their inline `legacy-name-deferred` annotations are the
source-level removal checklist.

## Evidence retained

On 2026-09-14, `https://caura.dev/install.sh` and
`https://caura.dev/memclaw/latest.txt` both returned HTTP 200. <!-- legacy-name-floor: measured current mirror path --> That evidence protects
the installer path layout; it does not make the retired hostname permanent.

The original policy investigation used read-only source/history, registry metadata, Cloud
Run mapping/log schema, and repository inventory checks. It found that source aliases,
published resolver objects, live infrastructure identifiers, and historical evidence have
different removal conditions. The compatibility register contains the dated findings and
current classifications; no infrastructure, package, DNS, database, secret, or live service
was changed by that investigation or by this documentation split.
