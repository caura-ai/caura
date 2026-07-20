"""Window formation — the cursor_to mechanics of the per-file protocol.

Server contract (verified, routes/interview.py:89-96): seqs strictly
ascending, ``seqs[0] >= cursor_from``, ``seqs[-1] <= cursor_to``. Events
need NOT reach cursor_to, so:

- a window that stops on a CAP sets ``cursor_to`` to the last INCLUDED
  event's seq (lines after it head the next window);
- the FINAL window of a scan sets ``cursor_to`` to the last complete line
  index — even when the tail is pure filtered noise — so the watermark
  advances past it and the cheap next-run skip can fire;
- zero events in the whole range → NO window (server requires ≥1 event);
  the noise tail is rescanned locally next run, at zero LLM/network cost.

Caps are sized to the server's synchronous 90s interview budget: the
real-LLM pilot measured ~63s for ~200k chars, so 150k chars ≈ 30%
headroom; 400 events stays under the 500 hard cap.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

from .parser import ParsedEvent

MAX_EVENTS_PER_WINDOW = 400
MAX_WINDOW_CHARS = 150_000
MIN_WINDOW_EVENTS = 10
MIN_WINDOW_CHARS = 2_000


@dataclass
class Window:
    cursor_from: int
    cursor_to: int
    events: list[ParsedEvent]

    @property
    def chars(self) -> int:
        return sum(len(e.content) for e in self.events)


def build_windows(
    events: list[ParsedEvent],
    *,
    cursor_from: int,
    eof_line_index: int,
    max_events: int = MAX_EVENTS_PER_WINDOW,
    max_chars: int = MAX_WINDOW_CHARS,
) -> Iterator[Window]:
    """Split a scan's events into submit windows.

    ``cursor_from`` is the first unconsumed line index (watermark + 1);
    ``eof_line_index`` is the last complete line the scan covered. Yields
    nothing when ``events`` is empty.
    """
    if not events:
        return
    start = cursor_from
    batch: list[ParsedEvent] = []
    chars = 0
    for event in events:
        event_len = len(event.content)
        if batch and (len(batch) >= max_events or chars + event_len > max_chars):
            # Cap-stop: cursor_to = last included event's seq.
            yield Window(cursor_from=start, cursor_to=batch[-1].seq, events=batch)
            start = batch[-1].seq + 1
            batch = []
            chars = 0
        batch.append(event)
        chars += event_len
    # Final window: advance past any filtered noise tail to EOF.
    yield Window(cursor_from=start, cursor_to=max(eof_line_index, batch[-1].seq), events=batch)


def window_is_worth_interviewing(
    window: Window,
    *,
    min_events: int = MIN_WINDOW_EVENTS,
    min_chars: int = MIN_WINDOW_CHARS,
    flush: bool = False,
) -> bool:
    """Gate dribbles: don't burn an LLM interview on a couple of lines.

    Only meaningful for the FINAL (typically small) window; callers pass
    ``flush=True`` to override (e.g. session-end hook draining a file).
    """
    if flush:
        return True
    return len(window.events) >= min_events or window.chars >= min_chars
