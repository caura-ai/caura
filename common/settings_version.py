"""Which settings a live write was decided under, checked where it is written (g2.8).

Why this module exists. core-api decides whether to hold a write from the
organization settings it caches per process. A settings change clears the cache
of the process that made it and broadcasts to every other, but a write can reach
a process the broadcast has not: on staging a write sent 170 ms after
``quarantine.below_trust_by_fleet`` was tightened went live, and the next two,
a second later, were held. A missed broadcast leaves a process on the old
settings until its cache entry expires, five minutes later.

So a write that goes live says which settings it was decided under, and storage
compares that with the settings row inside the transaction that inserts it. If
they differ, the insert is refused and core-api decides again under the current
settings. The check costs one primary-key read on the insert's own connection
and transaction, and no request between the services.

The version is the settings row's ``updated_at``, rendered by storage and opaque
to core-api, which only hands it back. Compared for equality, never order:
``updated_at`` is the time a settings transaction began, and one that waited on
another's row lock can commit a smaller value after it.

Pure data, like ``common/duplicate_memory.py`` and ``common/permanent_failure.py``,
and for their reason: two separately-deployed services have to agree on these
strings byte for byte, so they are written once.
"""

from __future__ import annotations

from datetime import datetime

# The key on a memory insert payload. Present only on a write that went live
# after a hold decision; storage checks nothing on a payload without it, so a
# core-api that predates it, and every write that is held or the platform's own,
# insert exactly as before.
SETTINGS_VERSION_KEY = "settings_version"

# ``detail.error`` on storage's 409 when the settings changed. A 409 from the
# insert routes otherwise means a duplicate, and the two need opposite answers:
# a duplicate is the caller's to resolve, a settings change is core-api's.
SETTINGS_CHANGED = "settings_changed"

# The version of an organization with no settings row. A string rather than
# ``None`` so that a write decided under "nothing set" still claims a version:
# the first settings ever written for a tenant must be able to hold its writes.
NO_SETTINGS = ""


def version_of(updated_at: datetime | None) -> str:
    """The version of a settings row whose ``updated_at`` is this, or of no row."""
    return updated_at.isoformat() if updated_at is not None else NO_SETTINGS
