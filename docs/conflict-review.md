# Apply a reviewed conflict decision

`PATCH /conflicts/{id}/resolve` applies supported `resolved` verdicts and
records the decision in one storage transaction. The existing reviewer gate
requires a signed-in person, an admin/standalone operator, or a registered
agent at trust level 2 or higher. Tenant and read-only credential checks run
before storage is called. An authorized reviewer can explicitly settle a
dispute whose lower-trust writer could not supersede a higher-trust fact
automatically.

```json
{
  "tenant_id": "tenant-a",
  "review_status": "resolved",
  "resolution_action": "supersede"
}
```

An omitted `resolution_action` accepts the stored detector proposal when it is
supported. The committed action is returned as `resolution_action` and used
in the review event and correction notification.

| Action | Applied effect |
|---|---|
| `supersede` | Retire the older proposition as `outdated`; point the newer one's `supersedes_id` to it. |
| `replace` | Mark the older proposition `conflicted` and its verification disputed; point the newer one's `supersedes_id` to it. Content is preserved. |
| `mark_disputed` | Mark both propositions `conflicted` with disputed verification; clear any supersession edge between them. |
| `no_op` | Clear the pair-owned edge and restore a detector-retired older proposition to `active` when its state still supports that undo. Already-live statuses are preserved. |

Direction follows detection: earlier `created_at` is the older proposition;
ties or unknown timestamps use the smaller UUID string. The triggering
`new_memory_id` can itself be older. Existing `active`, `pending` and
`confirmed` status on the surviving row is preserved. Correction notifications
use the committed loser/winner direction and include replacement text only
for `replace` and `supersede`.
Legacy hook calls that omit both an action and recorded reviewer-effect
metadata retain their historical summary behavior.

`scope`, `merge`, `split_entity`, `downweight` and `ask` require additional
resolution input or further work. They return 400 and leave the review pending.
A missing stored proposal is handled the same way. Their vocabulary remains
valid for detector proposals.

Both parent memories are locked in UUID order before the conflict is locked
and refreshed. Every lookup/update is tenant-bound. Missing/deleted, held,
archived or otherwise unavailable pair members, a changed external chain,
another successor/verdict still holding the pair, or a suppressed tenant
produce 409 without changing the review. A no-op cannot restore a later manual
status change. A missing/foreign conflict remains 404; an already reviewed
conflict remains 409. Pending-state compare-and-set permits one reviewer to
commit. Any failure between effects and the decision rolls everything back.

The detector's `action`, reason and provenance remain intact. Separate
`review_effect_applied`, `review_winner_id` and `review_loser_id` metadata record
the committed human effect. Retraction/holding checks honor that effect for
resolved rows while retaining the original detector evidence.

`dismissed` retains its existing undo path and reporting. Historical bulk
repair and operator scripts are separate work. This change performs no
historical-data sweep and needs no schema migration. Database regressions run
in isolated CI; local checks use only mock-backed unit tests.
