"""09/02 M-10 and M-11 — the model chain did not know which provider it was for.

`enrichment_model` / `contradiction_model` / `recall_model` are SHARED tenant
settings: one value, read by whichever provider happens to be active. Two
opposite defects fell out of that.

**M-11** — `resolve_openai_compatible` never accepted `model_attr`, so every
per-service model knob was inert on the default provider. A tenant could set
`contradiction_model` and nothing would read it: config that lies.

**M-10** — the reverse. Gemini *does* read the shared attribute, so a tenant who
configured `enrichment_model = "gpt-5.4-nano"` for OpenAI and then switched
provider handed Gemini an OpenAI model id. That 404s on every call, which is
why the documented Gemini setup never worked.
"""

from types import SimpleNamespace

import pytest

from common.llm._credentials import (
    _model_family,
    resolve_gemini_config,
    resolve_openai_compatible,
)
from common.llm.constants import (
    GEMINI_DEFAULT_MODEL,
    LLM_FALLBACK_MODEL_OPENAI,
)
from common.provider_names import ProviderName

pytestmark = pytest.mark.unit


def _cfg(**kw):
    kw.setdefault("openai_api_key", "sk-test")
    kw.setdefault("gemini_api_key", "gm-test")
    return SimpleNamespace(**kw)


# ── M-11: the per-service knobs actually resolve now ─────────────────────


def test_a_per_service_model_is_honoured_on_openai():
    """The defect: this setting existed and nothing read it."""
    _, _, model = resolve_openai_compatible(
        ProviderName.OPENAI,
        _cfg(contradiction_model="gpt-4o-mini"),
        model_attr="contradiction_model",
    )
    assert model == "gpt-4o-mini"


def test_different_services_can_resolve_different_models():
    """The whole point of per-service knobs — one tenant, two models."""
    cfg = _cfg(enrichment_model="gpt-4o", contradiction_model="gpt-4o-mini")
    _, _, enrich = resolve_openai_compatible(
        ProviderName.OPENAI, cfg, model_attr="enrichment_model"
    )
    _, _, contra = resolve_openai_compatible(
        ProviderName.OPENAI, cfg, model_attr="contradiction_model"
    )
    assert (enrich, contra) == ("gpt-4o", "gpt-4o-mini")


def test_an_unset_knob_still_falls_back_to_the_provider_default():
    """Tenants that never set one must behave exactly as before."""
    _, _, model = resolve_openai_compatible(
        ProviderName.OPENAI, _cfg(), model_attr="contradiction_model"
    )
    assert model == LLM_FALLBACK_MODEL_OPENAI


def test_the_registry_forwards_model_attr():
    """Guards the seam. The resolver accepting ``model_attr`` is inert unless
    the caller passes it, which is how the original defect survived."""
    import inspect

    from common.llm import registry

    src = inspect.getsource(registry.get_llm_provider)
    assert "model_attr=model_attr" in src
    assert "resolve_openai_compatible(name, tenant_config)" not in src


# ── M-10: a foreign model id is not sent ─────────────────────────────────


def test_gemini_does_not_receive_an_openai_model_id():
    """The 404. A tenant configured for OpenAI then switched to Gemini."""
    _, model = resolve_gemini_config(
        _cfg(enrichment_model="gpt-5.4-nano"), model_attr="enrichment_model"
    )
    assert model == GEMINI_DEFAULT_MODEL
    assert not model.startswith("gpt-")


def test_openai_does_not_receive_a_gemini_model_id():
    """Symmetric — the same mistake in the other direction."""
    _, _, model = resolve_openai_compatible(
        ProviderName.OPENAI,
        _cfg(enrichment_model="gemini-3.1-flash-lite-preview"),
        model_attr="enrichment_model",
    )
    assert model == LLM_FALLBACK_MODEL_OPENAI


def test_a_genuine_gemini_model_is_still_honoured():
    """The guard must not eat correct configuration."""
    _, model = resolve_gemini_config(
        _cfg(enrichment_model="gemini-2.5-pro"), model_attr="enrichment_model"
    )
    assert model == "gemini-2.5-pro"


@pytest.mark.parametrize(
    "model",
    [
        "my-company/finetune-v3",
        "anthropic/claude-sonnet-4",  # OpenRouter's vendor/model shape
        "llama-3.1-70b-instruct",
        "mistral-large",
    ],
)
def test_an_unrecognised_model_id_passes_through_untouched(model):
    """Deliberately conservative: override only on a POSITIVE match against
    another family. Custom deployments, fine-tunes and OpenRouter ids must not
    be second-guessed — "not recognised" means "leave it alone"."""
    _, _, resolved = resolve_openai_compatible(
        ProviderName.OPENROUTER, _cfg(openrouter_api_key="or-k", enrichment_model=model)
    )
    assert resolved == model


@pytest.mark.parametrize(
    "model,family",
    [
        ("gpt-4o", ProviderName.OPENAI),
        ("o3-mini", ProviderName.OPENAI),
        ("claude-sonnet-4", ProviderName.ANTHROPIC),
        ("gemini-2.5-flash", ProviderName.GEMINI),
        ("GPT-4O", ProviderName.OPENAI),
        ("totally-unknown", None),
    ],
)
def test_family_detection(model, family):
    assert _model_family(model) is family


def test_a_non_string_model_value_is_ignored():
    """``getattr`` on a loosely-typed config can yield a Mock, a sentinel or an
    int. A non-string model id is meaningless and would only fail deeper, inside
    the provider SDK, where the cause is far harder to see."""
    _, _, model = resolve_openai_compatible(
        ProviderName.OPENAI, _cfg(enrichment_model=object())
    )
    assert model == LLM_FALLBACK_MODEL_OPENAI


def test_the_override_is_logged_with_both_models(caplog):
    """Silently replacing an operator's explicit setting trades a 404 for a
    mystery. The log has to name what was rejected and what was used."""
    import logging

    with caplog.at_level(logging.WARNING):
        resolve_gemini_config(
            _cfg(enrichment_model="gpt-5.4-nano"), model_attr="enrichment_model"
        )
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "gpt-5.4-nano" in msg
    assert GEMINI_DEFAULT_MODEL in msg


def test_every_provider_branch_routes_through_the_shared_resolver():
    """Forward guard for providers added after this fix.

    Each branch of ``resolve_openai_compatible`` used to end in
    ``return key, <BASE_URL>, <DEFAULT_MODEL>`` — the shape that made M-11
    possible, since a hardcoded default silently ignores ``model_attr``. A new
    provider copied from an existing branch inherits that bug, and nothing
    would notice: the knob just quietly does nothing for that one provider.

    So the invariant is structural, not per-provider — every ``return`` that
    yields a model must take it from ``_model_for_provider``. This is expected
    to fail for any branch added without that call, which is the point.
    """
    import ast
    import inspect

    from common.llm import _credentials

    fn = ast.parse(inspect.getsource(_credentials.resolve_openai_compatible).lstrip())
    returns = [
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.Return)
        and isinstance(n.value, ast.Tuple)
        and len(n.value.elts) == 3
    ]
    assert returns, "expected the (key, base_url, model) returns"

    offenders = []
    for r in returns:
        model_expr = ast.unparse(r.value.elts[2])
        # The empty-credentials guard returns literals; it resolves no model.
        if model_expr in {'""', "''"}:
            continue
        if "_model_for_provider" not in model_expr and model_expr != "model":
            offenders.append(model_expr)
    assert not offenders, (
        "these provider branches return a hardcoded model instead of resolving "
        f"it through _model_for_provider, so model_attr is inert for them: {offenders}"
    )
