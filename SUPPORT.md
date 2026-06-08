# Getting support

How you reach us depends on what you need.

## Usage questions and discussion

For "how do I…", architecture sanity-checks, capacity questions, sharing what you built, or open-ended brainstorming — use [Discussions](https://github.com/caura-ai/caura-memclaw/discussions) or chat with us on [Discord](https://discord.com/invite/aNfpgfpj). The issue tracker is reserved for actionable bug reports and concrete feature proposals.

## Bug reports

Open a [Bug report](https://github.com/caura-ai/caura-memclaw/issues/new?template=bug_report.yml). The form requires the minimum fields we need to triage: version, deployment mode, OS, LLM provider, repro steps, and expected vs. actual behavior. Reports without those fields are slow to action.

Before filing:
- Search existing issues and Discussions for duplicates.
- Confirm the behavior contradicts the [Public API & Stability](README.md#public-api--stability) section. Behavior of internal surfaces (e.g. database schema, gateway-injected headers, `/admin/*` routes) can change without notice and isn't tracked as a bug.

## Feature requests

Open a [Feature request](https://github.com/caura-ai/caura-memclaw/issues/new?template=feature_request.yml). The form asks you to declare which surface from the SemVer contract the feature touches — this scopes the conversation early.

Out-of-scope contributions get a polite decline with a pointer to the relevant section of [CONTRIBUTING.md](CONTRIBUTING.md). We don't merge features out of guilt; the issue thread is where to make the case before writing code.

## Security vulnerabilities

**Do not open a public issue.** Use [GitHub Security Advisories](https://github.com/caura-ai/caura-memclaw/security/advisories/new) — reports are private to maintainers until a coordinated disclosure. Full policy in [SECURITY.md](SECURITY.md).

## Response expectations

We aim to acknowledge every issue and PR within five business days — not necessarily with a fix or merge, but with a triage decision (accepted / needs-info / out-of-scope) so you know what's happening. If a week goes by without any response, ping the thread or open a Discussion — we'd rather be reminded than ghost you.

## Maintainers and roles

See [MAINTAINERS.md](MAINTAINERS.md) for the current maintainer roster, areas of ownership, and how triage rotation works.
