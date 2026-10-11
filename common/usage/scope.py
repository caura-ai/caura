"""The ambient usage scope: whom the model calls in flight are made for.

A ``ContextVar`` rides the await chain (and ``asyncio.to_thread``, which
copies the context) from the place that knows the caller — a request
handler, a background task, a queue consumer — down to the provider, the
same way :data:`common.llm.call_context.llm_call_label` carries the
operation label. Nothing on the call path takes a new parameter.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any, ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")


@dataclass(frozen=True, slots=True)
class UsageScope:
    """Who the enclosed work is for. Every field may be unknown (None)."""

    tenant_id: str | None = None
    agent_id: str | None = None
    fleet_id: str | None = None
    #: What the work is, e.g. ``"write"``, ``"search"``, ``"crystallize"``.
    #: The provider-level label (``llm_call_label``) is more specific and
    #: wins for model calls; this names value records and unlabelled calls.
    operation: str | None = None


_scope: ContextVar[UsageScope | None] = ContextVar("usage_scope", default=None)


def current_scope() -> UsageScope | None:
    return _scope.get()


@contextmanager
def usage_scope(
    *,
    tenant_id: str | None = None,
    agent_id: str | None = None,
    fleet_id: str | None = None,
    operation: str | None = None,
) -> Iterator[UsageScope]:
    """Name whom the enclosed work is for.

    Fields left as None keep the enclosing scope's value, so an inner scope
    only says what it adds.
    """
    outer = _scope.get() or UsageScope()
    given = {"tenant_id": tenant_id, "agent_id": agent_id, "fleet_id": fleet_id, "operation": operation}
    scope = replace(outer, **{k: v for k, v in given.items() if v is not None})
    token = _scope.set(scope)
    try:
        yield scope
    finally:
        _scope.reset(token)


_Pick = str | Callable[[dict[str, Any]], Any] | None


def _pick(spec: _Pick, args: dict[str, Any]) -> str | None:
    if spec is None:
        return None
    value = spec(args) if callable(spec) else args.get(spec)
    return str(value) if value not in (None, "") else None


def scoped(
    operation: str | None = None,
    *,
    tenant: _Pick = None,
    agent: _Pick = None,
    fleet: _Pick = None,
) -> Callable[[Callable[P, Coroutine[Any, Any, R]]], Callable[P, Coroutine[Any, Any, R]]]:
    """Run an async function inside a :func:`usage_scope` taken from its arguments.

    Each of ``tenant``, ``agent`` and ``fleet`` names a parameter, or is a
    function of the bound arguments (``lambda a: a["data"].tenant_id``). One
    line on an entry point, instead of re-indenting its body under ``with``.
    """

    def wrap(fn: Callable[P, Coroutine[Any, Any, R]]) -> Callable[P, Coroutine[Any, Any, R]]:
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        async def run(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                bound = sig.bind_partial(*args, **kwargs)
                bound.apply_defaults()
                values = dict(bound.arguments)
                names = {
                    "tenant_id": _pick(tenant, values),
                    "agent_id": _pick(agent, values),
                    "fleet_id": _pick(fleet, values),
                }
            except Exception:  # metering never breaks the call
                names = {"tenant_id": None, "agent_id": None, "fleet_id": None}
            with usage_scope(operation=operation, **names):
                return await fn(*args, **kwargs)

        # The coroutine ``run`` produces carries ITS code object's name, which
        # is what task names, tracebacks and the swallowed-failure gate
        # (``coro.cr_code.co_qualname``) read. Give it the wrapped function's,
        # so decorating an entry point doesn't rename the work it schedules.
        code = run.__code__  # type: ignore[attr-defined]
        run.__code__ = code.replace(co_name=fn.__name__, co_qualname=fn.__qualname__)  # type: ignore[attr-defined]
        return run

    return wrap
