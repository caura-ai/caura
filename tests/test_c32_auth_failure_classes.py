"""C32 / API-05 — every auth refusal names its own reason.

``code_for_status`` maps every 403 to ``FORBIDDEN`` and every 401 to
``UNAUTHORIZED``, so the distinct reasons a request can be refused at the auth
boundary all arrived at the caller as one word. ``errors.py`` records what that
cost: an agent refused a write with a tenant key concluded that tenant keys
cannot write — false — and stopped trying. A wrong general rule learned from a
specific refusal is worse than no answer, because the agent stops asking.

#950 introduced ``coded_detail`` and converted 16 sites. This closes the
remaining 33, so all 49 refusals carry a code that names the reason.

The wire contract is unchanged. ``app.http_exception_handler`` flattens a
``coded_detail`` dict back to ``{"detail": <message>, "error": {"code", ...}}``,
so ``detail`` stays the same string it was; the code is additive.
"""

import ast
import pathlib

import pytest

from core_api import errors

pytestmark = pytest.mark.unit

SRC = pathlib.Path("core-api/src")


def _auth_raises():
    """Every ``raise HTTPException(401|403)`` in core-api, with its detail node."""
    out = []
    for path in sorted(SRC.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover - not our files to fix
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)):
                continue
            func = node.exc.func
            if (
                getattr(func, "id", None) or getattr(func, "attr", None)
            ) != "HTTPException":
                continue
            kw = {k.arg: k.value for k in node.exc.keywords}
            if getattr(kw.get("status_code"), "value", None) not in (401, 403):
                continue
            out.append((path, node.lineno, kw.get("detail")))
    return out


def _is_coded(detail) -> bool:
    if not isinstance(detail, ast.Call):
        return False
    f = detail.func
    return (getattr(f, "id", None) or getattr(f, "attr", None)) == "coded_detail"


def test_every_auth_refusal_carries_a_code():
    """The ratchet. A new bare-string 401/403 fails here rather than shipping a
    refusal the caller cannot act on."""
    bare = [
        f"{p.relative_to(SRC)}:{line}"
        for p, line, detail in _auth_raises()
        if not _is_coded(detail)
    ]
    assert bare == [], "auth refusals without an error code:\n  " + "\n  ".join(bare)


def test_the_census_is_not_vacuous():
    """A guard that passes because it found nothing is not a guard."""
    assert len(_auth_raises()) >= 45


# ── the codes themselves ──────────────────────────────────────────────────


def test_auth_codes_are_unique():
    """Two constants sharing a value would make the code ambiguous at exactly
    the moment a caller is trying to branch on it."""
    codes = {n: v for n, v in vars(errors).items() if n.startswith("AUTH_")}
    seen: dict[str, str] = {}
    dupes = []
    for name, value in sorted(codes.items()):
        if value in seen:
            dupes.append((value, seen[value], name))
        seen[value] = name
    assert dupes == []


def test_auth_codes_are_upper_snake():
    for name, value in vars(errors).items():
        if not name.startswith("AUTH_"):
            continue
        assert value == value.upper(), name
        assert " " not in value, name


def test_no_auth_code_collides_with_a_status_derived_code():
    """A code equal to ``FORBIDDEN`` or ``UNAUTHORIZED`` would be
    indistinguishable from the generic fallback it exists to replace."""
    generic = set(errors.STATUS_TO_CODE.values())
    auth = {v for n, v in vars(errors).items() if n.startswith("AUTH_")}
    assert auth & generic == set()


# ── the four inherited values ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "const,value",
    [
        ("AUTH_TENANT_MISMATCH", "TENANT_MISMATCH"),
        ("AUTH_UNAUTHENTICATED", "UNAUTHENTICATED"),
        ("AUTH_SKILLS_FACTORY_DISABLED", "SKILLS_FACTORY_DISABLED"),
        ("AUTH_SKILLS_INBOX_FORBIDDEN", "SKILLS_INBOX_FORBIDDEN"),
    ],
)
def test_inherited_codes_keep_their_shipped_spelling(const, value):
    """These four values are inherited, not chosen.

    ``skills_inbox`` and ``stm`` already shipped the token welded to the front
    of the prose — ``"TENANT_MISMATCH — this credential is not scoped…"`` — with
    a comment at the site saying clients branch on the prefix, not the message.
    Moving it into ``error.code`` has to keep the string byte-for-byte; a
    tidier spelling here is a silent breaking change for every caller already
    parsing the prefix.
    """
    assert getattr(errors, const) == value


def test_the_prose_prefix_still_matches_the_code_it_became():
    """Belt and braces on the above: the message these sites raise still opens
    with the same token, so a client reading either surface agrees."""
    src = (SRC / "core_api/routes/skills_inbox.py").read_text()
    assert f'"{errors.AUTH_TENANT_MISMATCH} —' in src
    assert f'"{errors.AUTH_UNAUTHENTICATED} —' in src


# ── the shape a caller receives ───────────────────────────────────────────


def test_coded_detail_is_flattened_by_the_handler_contract():
    """``detail`` must survive as the plain string clients already read; the
    code rides alongside in ``error``. This is what keeps C32 additive."""
    detail = errors.coded_detail(errors.AUTH_AGENT_TRUST_TOO_LOW, "nope", required=2)
    assert detail["code"] == "AGENT_TRUST_TOO_LOW"
    assert detail["message"] == "nope"
    assert detail["details"] == {"required": 2}

    body = {"detail": detail["message"], **errors.make_error_payload(**detail)}
    assert body["detail"] == "nope"
    assert body["error"]["code"] == "AGENT_TRUST_TOO_LOW"
