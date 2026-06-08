# Security Policy

## Reporting a Vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Use [GitHub Private Vulnerability Reporting](https://github.com/caura-ai/caura-memclaw/security/advisories/new) — the **"Report a vulnerability"** button on the repo's Security tab. Reports are private to maintainers until a coordinated disclosure.

Please include:

- A description of the issue and its impact
- Steps to reproduce, or a proof-of-concept
- The affected MemClaw version (call `GET /api/v1/version`, or `git rev-parse HEAD` if running from source)

If you cannot use GitHub Security Advisories, email **security@caura.ai** as a fallback. Mark the subject `[SECURITY]` and expect a slower acknowledgement than the form.

## Response Time

We aim to acknowledge reports within **3 business days** and to provide an
initial assessment (severity, tentative fix timeline) within **7 business days**.

## Coordinated Disclosure

We follow a coordinated disclosure process:

1. You report the vulnerability privately.
2. We confirm the issue and work on a fix.
3. We prepare a patched release and a public advisory.
4. We credit you in the advisory (unless you prefer to remain anonymous).
5. The advisory is published after the patched release is available.

We ask that you do not publicly disclose the issue until we have released a fix.

## Supported Versions

MemClaw follows [Semantic Versioning](https://semver.org/). Security fixes are
provided for:

| Version | Supported |
|---------|-----------|
| Latest minor release (2.x) | Yes |
| Previous minor release | Best-effort, critical fixes only |
| Older releases | No |

## Scope

In scope:

- MemClaw core API (`core-api/`)
- MemClaw storage API (`core-storage-api/`)
- OpenClaw plugin (`plugin/`)
- Default Docker images published from this repository

Out of scope:

- Third-party dependencies (report upstream)
- Self-hosted deployments that have been modified from the published release
- Denial-of-service via excessive resource usage when users control their own
  configuration
- Issues in integrations (e.g., a specific MCP client) that are not bugs in
  MemClaw itself

## Non-Security Bugs

For non-security bugs, please open a regular GitHub issue using the bug report
template.
