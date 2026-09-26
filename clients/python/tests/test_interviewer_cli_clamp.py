"""oss-0902-l-09 — ``--max-event-chars`` could be set past what the server takes.

The flag accepted any integer. The server truncates each event at
``INTERVIEW_EVENT_MAX_CHARS`` (8000) and REJECTS a window whose events exceed
it, and a rejected window never advances the cursor — so an over-large value did
not degrade the transcript, it stalled it permanently, 422 after 422.

Clamping is announced on stderr rather than applied quietly: a flag that is
silently overruled leaves the operator believing a setting that is not in force,
which is how this stayed unnoticed.
"""

import argparse

import pytest

from caura_client.interviewer import cli


def test_a_value_over_the_server_limit_is_clamped(capsys):
    assert cli._event_chars("20000") == cli._SERVER_MAX_EVENT_CHARS
    assert "exceeds the server limit" in capsys.readouterr().err


def test_clamping_names_both_the_asked_and_the_used_value(capsys):
    cli._event_chars("999999")
    err = capsys.readouterr().err
    assert "999999" in err and str(cli._SERVER_MAX_EVENT_CHARS) in err


def test_values_within_the_limit_pass_through_silently(capsys):
    assert cli._event_chars("4000") == 4_000
    assert cli._event_chars(str(cli._SERVER_MAX_EVENT_CHARS)) == cli._SERVER_MAX_EVENT_CHARS
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("bad", ["0", "-5"])
def test_a_nonsense_value_is_refused_rather_than_clamped(bad):
    """Zero or negative is a mistake, not an over-ask. Clamping it upward would
    invent a value the caller never asked for."""
    with pytest.raises(argparse.ArgumentTypeError):
        cli._event_chars(bad)


def test_both_subcommands_use_the_clamp():
    """``run`` and ``hook`` both take the flag; clamping one leaves the other
    able to stall a transcript."""
    import inspect

    src = inspect.getsource(cli)
    assert src.count('"--max-event-chars", type=_event_chars') == 2


def test_the_limit_is_documented_as_mirroring_the_server():
    """``caura_client`` is a separate distribution and cannot import core-api, so
    this constant is a COPY of ``INTERVIEW_EVENT_MAX_CHARS`` and can drift. The
    comment naming its source is the only link between them; keep it."""
    import inspect

    src = inspect.getsource(cli)
    i = src.index("_SERVER_MAX_EVENT_CHARS = 8_000")
    assert "INTERVIEW_EVENT_MAX_CHARS" in src[max(0, i - 700) : i]
