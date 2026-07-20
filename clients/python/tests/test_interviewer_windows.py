"""Window-formation tests — the cursor_to mechanics the protocol rests on."""

from __future__ import annotations

from memclaw_client.interviewer.parser import ParsedEvent
from memclaw_client.interviewer.windows import (
    Window,
    build_windows,
    window_is_worth_interviewing,
)


def _event(seq: int, chars: int = 100) -> ParsedEvent:
    return ParsedEvent(
        seq=seq,
        ts="2026-07-19T10:00:00.000Z",
        session_id="p:s1",
        role="user",
        kind="prompt",
        content="x" * chars,
    )


def test_single_window_eof_cursor_advances_past_noise_tail():
    # Events at lines 5 and 7; lines 8..20 are filtered noise. cursor_to
    # must reach EOF so the watermark clears the tail.
    events = [_event(5), _event(7)]
    windows = list(build_windows(events, cursor_from=0, eof_line_index=20))
    assert len(windows) == 1
    assert windows[0].cursor_from == 0
    assert windows[0].cursor_to == 20
    assert [e.seq for e in windows[0].events] == [5, 7]


def test_event_cap_split_sets_cursor_to_last_included_seq():
    events = [_event(i) for i in range(10)]
    windows = list(build_windows(events, cursor_from=0, eof_line_index=9, max_events=4))
    assert [(w.cursor_from, w.cursor_to) for w in windows] == [(0, 3), (4, 7), (8, 9)]
    # Contiguous, no gap and no overlap.
    for prev, cur in zip(windows, windows[1:]):
        assert cur.cursor_from == prev.cursor_to + 1


def test_char_cap_split():
    events = [_event(i, chars=600) for i in range(5)]
    windows = list(build_windows(events, cursor_from=0, eof_line_index=4, max_chars=1500))
    # 600*2=1200 fits, third would be 1800 > 1500 → split after 2.
    assert [len(w.events) for w in windows] == [2, 2, 1]


def test_zero_events_yields_no_windows():
    assert list(build_windows([], cursor_from=0, eof_line_index=50)) == []


def test_sparse_seqs_stay_within_cursor_range():
    events = [_event(3), _event(9), _event(11)]
    (window,) = build_windows(events, cursor_from=2, eof_line_index=15)
    seqs = [e.seq for e in window.events]
    assert seqs == sorted(seqs)
    assert seqs[0] >= window.cursor_from
    assert seqs[-1] <= window.cursor_to  # the server's exact validation rules


def test_dribble_gate_and_flush():
    small = Window(cursor_from=0, cursor_to=3, events=[_event(1, chars=50)])
    assert not window_is_worth_interviewing(small)
    assert window_is_worth_interviewing(small, flush=True)
    big_chars = Window(cursor_from=0, cursor_to=3, events=[_event(1, chars=5000)])
    assert window_is_worth_interviewing(big_chars)  # min_chars path
    many = Window(cursor_from=0, cursor_to=30, events=[_event(i, chars=30) for i in range(12)])
    assert window_is_worth_interviewing(many)  # min_events path
