"""Fake a stdlib function for ONE importer instead of for the process.

``monkeypatch.setattr(some_module.asyncio, "sleep", fake)`` does not patch
``some_module``'s sleep. ``some_module.asyncio`` IS the one ``asyncio``
module object, so that line replaces ``asyncio.sleep`` for every importer
alive — the code under test, every other module, and every task already
running. The same is true of ``patch("pkg.mod.asyncio.sleep", ...)``: the
dotted string resolves the same shared object.

That is how caura#863's CI failure happened. ``test_llm_retry_after``
recorded sleeps into a list and asserted the list was ``[1.0, 2.0]``; it
got ``[0.7359102269208245, 1.0, 2.0]``, and the test's own captured log
named the intruder::

    WARNING common.http_retry storage_client.POST /memories/similar-candidates:
            ConnectError on attempt 3/5, retrying in 0.74s

A post-commit ``detect_contradictions_async`` task, left running by an
earlier test, retrying a storage-api that does not exist in CI. Its third
backoff came due while the recorder was installed, so an unrelated task's
sleep was reported as the retry loop's.

Patching the module REFERENCE instead of the module CONTENTS is the whole
fix: give the importer under test its own copy, and nobody else's sleep
can reach the recorder::

    monkeypatch.setattr(mod, "asyncio", scoped(asyncio, sleep=fake))
    patch.object(mod, "asyncio", scoped(asyncio, sleep=fake))

A real ``ModuleType`` carrying a shallow copy of the original namespace,
rather than a stub or a forwarding proxy: everything the importer reaches
for keeps working whether or not this file anticipated it. ``retry.py``
uses ``asyncio.wait_for`` beside its ``sleep``, and ``memory_service`` uses
nine asyncio names; an explicit stub would have to list them and would
break — silently, in the wait_for case — the next time one was added.

The copy is a snapshot. That is fine for stdlib modules, and wrong for a
module whose attributes a test rebinds later; use ``monkeypatch`` directly
for those.
"""

from __future__ import annotations

import types


def scoped(real: types.ModuleType, **overrides: object) -> types.ModuleType:
    """A private copy of *real* with *overrides* applied.

    Assign it over the importing module's own reference, so the override is
    visible to that importer and to nothing else.
    """
    clone = types.ModuleType(real.__name__)
    clone.__dict__.update(real.__dict__)
    clone.__dict__.update(overrides)
    return clone
