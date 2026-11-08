"""The seed google-genai sends, fitted to the API's signed 32-bit field."""

from __future__ import annotations

_INT32_MIN = -(2**31)
_INT32_MAX = 2**31 - 1


def genai_seed(seed: int | None) -> int | None:
    """``seed`` as ``GenerateContentConfig.seed`` can carry it.

    The field is a signed int32 on the wire, and Vertex refuses a request whose
    seed is outside it: ``400 INVALID_ARGUMENT: Invalid value at
    'generation_config.seed' (TYPE_INT32)``. Callers derive seeds from hashes,
    and entity extraction's is ``zlib.crc32``, which is unsigned and needs all
    32 bits for about half of all prompts. Every one of those was refused, and
    the extraction fell back to the regex heuristic.

    A seed that fits is sent unchanged, so the prompts that worked keep the
    answers they were getting. One that does not keeps its low 31 bits: still
    deterministic, so a re-ask repeats, which is what the caller pinned it for.
    """
    if seed is None or _INT32_MIN <= seed <= _INT32_MAX:
        return seed
    return seed & _INT32_MAX
