"""L-181: the extraction worker embeds only the entity names that need a vector.

It embedded every extracted name before resolving any, one provider call per
name, though a name that resolves by name (an exact or normalised match) needs
no vector: storage compares embeddings only for an item still unmatched, and a
matched row keeps the vector it has. The worker now resolves by name first,
embeds what that leaves in one batch call, and resolves those by similarity.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from core_api.services import entity_extraction_worker as w
from core_api.services.entity_extraction import ExtractedEntity, ExtractedGraph
from tests.conftest import close_scheduled_coro

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_CONFIG = SimpleNamespace(
    entity_blocklist=frozenset(), auto_entity_linking_enabled=False
)
_VECTOR = [0.1] * 8
_ACME = str(uuid4())
_GLOBEX = str(uuid4())


def _row(entity_id: str, name: str, matched_by: str) -> dict:
    return {
        "entity_id": entity_id,
        "canonical_name": name,
        "attributes": {},
        "matched_by": matched_by,
        "similarity": 1.0,
    }


async def _resolve(*, tenant_id, items, threshold):
    """Storage's precedence: a name match needs nothing else, while a
    similarity match needs the item's vector."""
    out = []
    for item in items:
        if item["canonical_name"] == "acme":
            out.append(_row(_ACME, "acme", "exact"))
        elif item["canonical_name"] == "globex industries" and item["name_embedding"]:
            out.append(_row(_GLOBEX, "globex", "similarity"))
        else:
            out.append(None)
    return out


def _upserted(items: list[dict]) -> list[dict]:
    return [
        {
            "input_idx": item["input_idx"],
            "entity_id": item.get("entity_id") or str(uuid4()),
            "action": "updated" if item["action"] == "update" else "created",
        }
        for item in items
    ]


def _sc() -> MagicMock:
    sc = MagicMock()
    sc.get_memory = AsyncMock(return_value={"id": "m", "deleted_at": None})
    sc.bulk_resolve_entities = AsyncMock(side_effect=_resolve)
    sc.bulk_upsert_entities = AsyncMock(side_effect=_upserted)
    sc.bulk_upsert_entity_links = AsyncMock(
        side_effect=lambda tenant_id, items: [
            {"input_idx": i["input_idx"], "created": True} for i in items
        ]
    )
    sc.set_subject_entity_if_null = AsyncMock(return_value=True)
    sc.set_predicate_if_null = AsyncMock(return_value=True)
    return sc


def _embedders(
    batch_error: Exception | None = None, unembeddable: str = ""
) -> tuple[AsyncMock, AsyncMock]:
    """``get_embeddings_batch`` and ``get_embedding`` stand-ins."""
    batch = AsyncMock(
        side_effect=batch_error or (lambda names, *_a, **_k: [_VECTOR] * len(names))
    )
    single = AsyncMock(
        side_effect=lambda name, *_a, **_k: None if name == unembeddable else _VECTOR
    )
    return batch, single


async def _run(
    sc: MagicMock, names: list[str], batch: AsyncMock, single: AsyncMock
) -> None:
    graph = ExtractedGraph(
        entities=[
            ExtractedEntity(canonical_name=name, entity_type="organization")
            for name in names
        ]
    )
    with (
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=_CONFIG),
        ),
        patch.object(
            w, "extract_entities_from_content", new=AsyncMock(return_value=graph)
        ),
        patch.object(w, "get_storage_client", return_value=sc),
        # ``create``: main's worker has no batch embedder to replace.
        patch.object(w, "get_embeddings_batch", new=batch, create=True),
        patch.object(w, "get_embedding", new=single),
        patch.object(w, "log_action", new=AsyncMock()),
        patch.object(w, "upsert_relation", new=AsyncMock()),
        patch("core_api.tasks.track_task", side_effect=close_scheduled_coro),
    ):
        await w.process_entity_extraction(uuid4(), "t1", None, "a1", "text", "fact")


def _resolve_calls(sc: MagicMock) -> list[list[tuple[str, list[float] | None]]]:
    return [
        [
            (item["canonical_name"], item["name_embedding"])
            for item in call.kwargs["items"]
        ]
        for call in sc.bulk_resolve_entities.await_args_list
    ]


async def test_a_name_that_resolves_by_name_is_not_embedded():
    sc = _sc()
    batch, single = _embedders()

    await _run(sc, ["acme", "initech"], batch, single)

    batch.assert_awaited_once()
    assert batch.await_args.args[0] == ["initech"]
    single.assert_not_awaited()


async def test_nothing_is_embedded_when_every_name_resolves_by_name():
    sc = _sc()
    batch, single = _embedders()

    await _run(sc, ["acme"], batch, single)

    batch.assert_not_awaited()
    single.assert_not_awaited()
    assert _resolve_calls(sc) == [[("acme", None)]]


async def test_the_names_left_are_embedded_in_one_background_call():
    sc = _sc()
    batch, single = _embedders()

    await _run(sc, ["initech", "hooli", "umbrella"], batch, single)

    batch.assert_awaited_once()
    assert batch.await_args.args[0] == ["initech", "hooli", "umbrella"]
    assert batch.await_args.kwargs["background"] is True
    single.assert_not_awaited()


async def test_the_name_pass_sends_no_vector_and_the_similarity_pass_the_rest():
    """The similarity match is still found and still wins first-seen-wins;
    the name match's update carries no vector, so its row keeps its own."""
    sc = _sc()
    batch, single = _embedders()

    await _run(sc, ["acme", "globex industries"], batch, single)

    assert _resolve_calls(sc) == [
        [("acme", None), ("globex industries", None)],
        [("globex industries", _VECTOR)],
    ]
    acme, globex = sc.bulk_upsert_entities.await_args.kwargs["items"]
    assert (acme["entity_id"], "name_embedding" in acme) == (_ACME, False)
    assert (globex["entity_id"], globex["canonical_name"]) == (_GLOBEX, "globex")
    assert globex["name_embedding"] == _VECTOR


async def test_a_failed_batch_falls_back_to_one_call_per_name():
    """``get_embeddings_batch`` raises where ``get_embedding`` degrades, so a
    failed batch must not leave every name without a vector: one bad name still
    costs only its own."""
    sc = _sc()
    batch, single = _embedders(
        batch_error=RuntimeError("provider down"), unembeddable="hooli"
    )

    await _run(sc, ["initech", "hooli"], batch, single)

    batch.assert_awaited_once()
    embedded_alone = sorted(call.args[0] for call in single.await_args_list)
    assert embedded_alone == ["hooli", "initech"]
    assert _resolve_calls(sc)[-1] == [("initech", _VECTOR)]
