"""Abbreviations a memory defines for a name, found without an LLM.

"The Ralenix Enterprise plan, or REP, ..." and "The Quillhaven response desk
(QRD) ..." define REP and QRD. A later memory that says only "REP now carries a
setup fee of $1,120" names the same thing, but entity resolution matched it on
the name ``rep`` and minted a second entity. The two facts then had different
subjects, so the Path C preflight dropped the pair, the entity-aware judge
called the subjects different, and a correction written with the abbreviation
never superseded the fact it corrects (Fleet Memory Bench: 0.23 of such
corrections right, against 0.56 for plain ones).

Two deterministic halves, used by the extraction worker:
- :func:`defined_abbreviations`: when a memory defines an abbreviation for one of
  its extracted names, the abbreviation becomes an alias of that entity, and a
  bracketed abbreviation is not kept in the canonical name (it is not a
  qualifier such as "acme (delaware)": its letters are the name's initials).
- :func:`looks_like_abbreviation`: a name the worker could not resolve that is a
  bare abbreviation written in capitals in the memory is looked up among the
  fleet's entity aliases.
"""

from __future__ import annotations

import re

# "X (ABC)", "X, or ABC", "X, internally called ABC", "X, known as ABC", "X, aka ABC"
_DEFINITION = re.compile(
    r"\(([A-Z][A-Z0-9]{1,5})\)"
    r"|,\s*(?:or|internally called|called|known as|aka|a\.k\.a\.)\s+([A-Z][A-Z0-9]{1,5})\b"
)
_SKIP = frozenset({"of", "the", "and", "for", "a", "an", "to", "on", "in", "at", "&"})


def initials(name: str) -> str:
    """Initial letters of a lowercase name's words, skipping short function words."""
    return "".join(w[0] for w in re.findall(r"[a-z0-9]+", name.lower()) if w not in _SKIP)


def _strip_bracket(name: str, abbr: str) -> str:
    return re.sub(rf"\s*\({re.escape(abbr)}\)\s*$", "", name).strip()


def defined_abbreviations(content: str, names: list[str]) -> dict[str, tuple[str, str]]:
    """``{name: (full_name, abbreviation)}`` for each extracted name the content
    defines an abbreviation for. ``full_name`` drops a trailing "(abbr)"; the
    abbreviation is lowercase. The abbreviation must be the name's initials, or
    the initials of a suffix of it ("talvori designated customer liaison" ->
    "dcl"), so an unrelated capitalised word is never taken for one."""
    abbrs = {(m.group(1) or m.group(2)).lower() for m in _DEFINITION.finditer(content)}
    out: dict[str, tuple[str, str]] = {}
    for name in names:
        for abbr in abbrs:
            full = _strip_bracket(name, abbr)
            if not full or full == abbr:
                continue
            ini = initials(full)
            if ini == abbr or (len(abbr) >= 2 and ini.endswith(abbr)):
                out[name] = (full, abbr)
                break
    return out


def looks_like_abbreviation(name: str, content: str) -> bool:
    """A bare 2-6 character name the content writes in capitals ("REP")."""
    if not re.fullmatch(r"[a-z][a-z0-9]{1,5}", name):
        return False
    return re.search(rf"\b{re.escape(name.upper())}\b", content) is not None
