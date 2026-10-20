# Enterprise staging notifier credential cutover (ENT2-019)

Preparation only. Keep the companion PR draft until the following nonsecret
setup is verified and Eldad approves activating the new credential consumer.
No App, environment, secret or installation change is made by this source PR.

1. Configure `enterprise-staging-notifier` in `caura-ai/caura` with selected
   branch policies: exactly one branch rule `main`, no tag/PR rules;
   `protected_branches: false`, `custom_branch_policies: true`.
   Environment creation/restriction needs a separately approved exact scope.
2. The operator creates/installs the dedicated `caura-enterprise-notifier` App
   on **only caura-enterprise**, with Contents write and implicit Metadata read.
   No PR write, Actions write, organization permissions, webhooks or bypass.
   Verify actual public App/installation IDs and permissions, not its name.
3. After the main-only boundary is verified, the operator stores
   `ENTERPRISE_NOTIFIER_APP_ID` and `ENTERPRISE_NOTIFIER_APP_PRIVATE_KEY` only
   in that environment, directly in GitHub. No values go through an agent or
   transcript; no same-name repository/org fallback. Do not reuse the shared key.
4. Record a reviewed activation/verification scope before human merge. Push to
   main runs this notifier; its repository_dispatch starts
   `bump-collaboration-pin.yml`, which may create/update `bot/collaboration-pin`
   and its PR using the separate enterprise writer App. The receiver resolves
   OSS main independently of the payload. Staging deploys after a human merges
   that pin PR to dev. This is repository automation, not a read-only credential test.
   Any token-mint diagnostic or live dispatch needs its own named approval under
   the enterprise credential-cutover runbook. Agents never merge or auto-merge.

The main-only environment controls which workflow ref may receive the key.
Manual triggers must run from main; the existing explicit `oss_ref` payload
behavior remains. No checkout or unreviewed repository code runs with the key.
Contents write permits more than dispatch, including ref writes and merging an
already-approved PR under existing rules. Token inputs narrow issued tokens;
the separately scoped App/installation isolates the underlying private key.

The existing dispatcher payload, retry loop and concurrency behavior are
preserved. A successful POST proves only GitHub accepted the dispatch; verify
the receiving enterprise run and its source/admission result separately.
Inspect token post-job revocation explicitly. Cancellation or a lost response
is not confirmed cleanup/delivery; do not blindly rerun the notification.

OSS release-please still uses its existing shared App. Do not switch it to this
notifier or to the read-only reader. Preserve unrelated repository access.
After enterprise, Ops and this consumer work, prepare the exact separate
withdrawal of old shared-App enterprise installation/org-secret access and
reconcile in-flight runs, bot approvals and armed auto-merges. This PR alone
does not close ENT2-019 or change normal team merge permissions.
