"""Which error codes an MCP tool handler can actually put on the wire.

Derived from source rather than declared, so ``ToolSpec.error_codes`` can be
compared against something independent of itself. Lives in ``tests/_*.py``
because this scan — not the assertions built on it — is the part that can be
wrong, and that shape of file is the one the review pipeline reads.

FOUR PATHS REACH A CALLER, and a scan that follows fewer than all four
under-reports, which is the failure mode that passes:

1. ``_error_response("CODE", …)`` called inside the handler.
2. A dict literal carrying ``"code": "CODE"`` — ``caura_manage`` builds its
   unknown-op envelope that way rather than through the helper.
3. A module-level PRE-BAKED ``CallToolResult``. ``_check_auth`` does not
   format an error; it returns ``_AUTH_ERROR`` / ``_ADMIN_ERROR``, both
   assigned at import time, and ``_check_write_scope`` returns
   ``_READ_ONLY_ERROR``. Every handler calls ``_check_auth``, so these are
   the codes MOST widely emitted and the ones a function-scoped walk misses
   entirely — the first version of this scan missed 19 of 52 that way, and
   reported ``UNAUTHORIZED`` as emitted by no tool at all.
4. A code passed by NAME rather than spelled: ``_check_plan_limit`` calls
   ``_error_response(AUTH_PLAN_LIMIT, …)`` with a constant imported from
   ``core_api.errors``. A literal-only scan reported ``PLAN_LIMIT_READ_ONLY``
   as emitted by no tool (L-138); the name is resolved through ``mcp_server``.

``emitted_codes`` follows paths 1-4 through helper calls within ``mcp_server``.
TWO MORE PATHS reach a caller from outside the handler (L-138), and each has
its own entry point below:

5. The dispatcher. ``_InstrumentedMCPServer.call_tool`` runs every handler
   under the request budget and answers ``REQUEST_BUDGET_EXCEEDED`` for
   whichever tool overran it — a code no handler mentions
   (``dispatch_codes``).
6. The service layer. A handler's ``except HTTPException`` turns whatever it
   caught into ``_detail_code``'s code: the detail's own ``code`` when it
   carries one (``coded_detail``), else the code its status maps to. So every
   ``HTTPException`` raised below such a ``try`` is a code the tool can
   answer — ``CONFLICT`` and ``DUPLICATE_MEMORY`` on ``caura_write``,
   ``AGENT_TRUST_TOO_LOW`` on ``caura_manage`` (``service_codes``).

Path 6 is a whole-program walk, so where it stops matters as much as what it
finds. It follows calls whose target resolves to a function or class defined
in ``core_api`` — through module globals and function-local imports, into a
class's methods when the class is called (which is how a write reaches
``CheckSemanticDuplicate``: compositions instantiate their steps), and through
a raise helper's parameters at each call site that passes them. It does NOT
follow a method called on an object (``sc.get_memory()``,
``pipeline.run()``): nothing in the source says which class the object is.
Of those, the storage client raises ``httpx`` errors rather than
``HTTPException``, so they never reach ``_detail_code`` anyway. It may
over-report: a code raised below and caught on the way up still counts. And a
raise it reaches but cannot resolve is reported, never dropped
(``unresolved_raises``) — a code the scan cannot name is exactly the code a
manifest would silently leave out.
"""

from __future__ import annotations

import ast
import builtins
import functools
import importlib
import importlib.util
import inspect
import sys

from starlette.exceptions import HTTPException

from core_api.errors import code_for_status

# Callables that format an error envelope from a code as their first argument.
_FORMATTERS = frozenset({"_error_response", "make_error_payload"})


def _named_code(node: ast.AST) -> str | None:
    """The code a NAME stands for in ``mcp_server`` (path 4), if it is one."""
    if not isinstance(node, ast.Name):
        return None
    from core_api import mcp_server

    value = getattr(mcp_server, node.id, None)
    return value if isinstance(value, str) and value.isupper() else None


def _literal_codes(node: ast.AST) -> set[str]:
    """Codes written as literals, or passed by name, under ``node`` (1, 2, 4)."""
    found: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            name = getattr(sub.func, "id", None) or getattr(sub.func, "attr", None)
            if name in _FORMATTERS and sub.args:
                first = sub.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    found.add(first.value)
                elif (code := _named_code(first)) is not None:
                    found.add(code)
        elif isinstance(sub, ast.Dict):
            # Always parallel: a ``**expr`` unpacking yields a None KEY, not a
            # missing one, so a length mismatch would mean this assumption is wrong.
            for key, value in zip(sub.keys, sub.values, strict=True):
                if not (isinstance(key, ast.Constant) and key.value == "code"):
                    continue
                if (
                    isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                    and value.value.isupper()
                ):
                    found.add(value.value)
                elif (code := _named_code(value)) is not None:
                    found.add(code)
    return found


@functools.cache
def _module() -> tuple[dict[str, ast.AST], dict[str, frozenset[str]]]:
    """``(functions by name, pre-baked error constants by name)``."""
    from core_api import mcp_server

    tree = ast.parse(inspect.getsource(mcp_server))
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    # Path 3. Top-level assignments only: a pre-baked result is built once at
    # import, which is exactly why it is invisible to a per-function walk.
    constants: dict[str, frozenset[str]] = {}
    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign):
            continue
        codes = _literal_codes(stmt.value)
        if not codes:
            continue
        for target in stmt.targets:
            if isinstance(target, ast.Name):
                constants[target.id] = frozenset(codes)
    return functions, constants


def prebaked_error_constants() -> dict[str, frozenset[str]]:
    """The path-3 constants, exposed so a test can pin that they are found."""
    return dict(_module()[1])


def _names(node: ast.AST) -> set[str]:
    return {sub.id for sub in ast.walk(node) if isinstance(sub, ast.Name)}


def _calls(node: ast.AST) -> set[str]:
    out: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            name = getattr(sub.func, "id", None) or getattr(sub.func, "attr", None)
            if name:
                out.add(name)
    return out


def emitted_codes(
    function_name: str, _seen: frozenset[str] = frozenset()
) -> frozenset[str]:
    """Every code reachable from ``function_name`` inside ``mcp_server``.

    ``_seen`` guards a cycle the graph does not currently have: measured, the
    only one is ``call_tool`` calling itself, and the ``callee !=
    function_name`` test below already covers that. It stays because mutual
    recursion would make this hang, and a depth limit would truncate quietly
    instead of terminating.
    """
    functions, constants = _module()
    if function_name in _seen or function_name not in functions:
        return frozenset()
    node = functions[function_name]
    codes = set(_literal_codes(node))
    for name in _names(node):
        codes |= constants.get(name, frozenset())
    for callee in _calls(node):
        if callee in functions and callee != function_name:
            codes |= emitted_codes(callee, _seen | {function_name})
    return frozenset(codes)


# --- path 5: the dispatcher -------------------------------------------------


def dispatch_codes() -> frozenset[str]:
    """Codes ``call_tool`` can answer for whichever tool it dispatched."""
    return emitted_codes("call_tool")


# --- path 6: the service layer ----------------------------------------------

_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)
_MISSING = object()
# A detail that carries a ``code`` the scan cannot name, as opposed to one
# that carries none (and so falls back to the status, as ``_detail_code`` does).
_UNNAMED = object()
# Text-building calls whose result is never a dict, so never carries a code.
_TEXT_METHODS = frozenset({"format", "join", "removeprefix", "replace", "strip"})


def _is_http_exception(target: object) -> bool:
    return isinstance(target, type) and issubclass(target, HTTPException)


def _child_blocks(stmt: ast.stmt) -> list[list[ast.stmt]]:
    """The statement lists under a compound statement (``if``, ``try``, …)."""
    blocks = [getattr(stmt, f, None) for f in ("body", "orelse", "finalbody")]
    blocks += [h.body for h in getattr(stmt, "handlers", [])]
    return [b for b in blocks if isinstance(b, list)]


@functools.cache
def _definitions(module_name: str) -> dict[str, ast.AST]:
    """``{__qualname__: def or class node}`` for one module, from its source."""
    index: dict[str, ast.AST] = {}

    def visit(body: list[ast.stmt], prefix: str) -> None:
        for stmt in body:
            if isinstance(stmt, _DEFS):
                index[prefix + stmt.name] = stmt
                visit(stmt.body, f"{prefix}{stmt.name}.<locals>.")
            elif isinstance(stmt, ast.ClassDef):
                index[prefix + stmt.name] = stmt
                visit(stmt.body, f"{prefix}{stmt.name}.")
            else:
                for block in _child_blocks(stmt):
                    visit(block, prefix)

    visit(ast.parse(inspect.getsource(sys.modules[module_name])).body, "")
    return index


def _definition(
    target: object, packages: tuple[str, ...] = ("core_api",)
) -> tuple[str, str] | None:
    """``(module, qualname)`` of a function or class defined in ``packages``."""
    try:
        # A decorator built with functools.wraps leaves the real body here.
        target = inspect.unwrap(target)
    except Exception:  # a cycle, or an attribute probe a proxy refuses
        return None
    if not (inspect.isfunction(target) or inspect.isclass(target)):
        return None
    module_name = getattr(target, "__module__", None) or ""
    if module_name.split(".")[0] not in packages or module_name not in sys.modules:
        return None
    if target.__qualname__ not in _definitions(module_name):
        return None
    return module_name, target.__qualname__


def _methods(cls: type) -> list[tuple[str, str]]:
    """The methods ``cls`` defines or inherits from ``core_api``."""
    found = []
    for klass in inspect.getmro(cls):
        owner = _definition(klass)
        if owner is None:
            continue
        for stmt in _definitions(owner[0])[owner[1]].body:
            if isinstance(stmt, _DEFS):
                found.append((owner[0], f"{owner[1]}.{stmt.name}"))
    return found


def _params(node: ast.AST) -> list[str]:
    a = node.args
    names = [p.arg for p in (*a.posonlyargs, *a.args, *a.kwonlyargs)]
    return names + [p.arg for p in (a.vararg, a.kwarg) if p is not None]


class _Scope:
    """Name resolution inside one function: its own imports, its locals (which
    resolve to nothing), then its module's globals, then builtins."""

    def __init__(self, module_name: str, node: ast.AST) -> None:
        self.module_name = module_name
        self.globals = vars(sys.modules[module_name])
        self.imports = self._local_imports(node)
        self.locals = {
            sub.id
            for sub in ast.walk(node)
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store)
        }
        if isinstance(node, _DEFS):
            self.locals.update(_params(node))
        self.locals -= set(self.imports)

    def _local_imports(self, node: ast.AST) -> dict[str, object]:
        package = sys.modules[self.module_name].__package__ or ""
        found: dict[str, object] = {}
        for sub in ast.walk(node):
            if isinstance(sub, ast.ImportFrom):
                base = sub.module or ""
                if sub.level:
                    base = importlib.util.resolve_name("." * sub.level + base, package)
                for alias in sub.names:
                    found[alias.asname or alias.name] = self._import(base, alias.name)
            elif isinstance(sub, ast.Import):
                for alias in sub.names:
                    if alias.asname:
                        found[alias.asname] = self._import(alias.name)
                    else:
                        top = alias.name.split(".")[0]
                        found[top] = self._import(top)
        return found

    @staticmethod
    def _import(module_name: str, attr: str | None = None) -> object:
        # Only the packages a code can come from: core_api raises, and
        # ``common`` builds some of the details it raises with. Anything else
        # is never followed, so importing it would only risk an optional
        # dependency the test environment does not install.
        if module_name.split(".")[0] not in ("core_api", "common"):
            return _MISSING
        module = importlib.import_module(module_name)
        if attr is None:
            return module
        value = getattr(module, attr, _MISSING)
        submodule = f"{module_name}.{attr}"
        # ``from package import submodule`` before anything imported it.
        if (
            value is _MISSING
            and hasattr(module, "__path__")
            and importlib.util.find_spec(submodule) is not None
        ):
            value = importlib.import_module(submodule)
        return value

    def resolve(self, node: ast.AST) -> object:
        if isinstance(node, ast.Name):
            if node.id in self.imports:
                return self.imports[node.id]
            if node.id in self.locals:
                return _MISSING
            if node.id in self.globals:
                return self.globals[node.id]
            return getattr(builtins, node.id, _MISSING)
        if isinstance(node, ast.Attribute):
            base = self.resolve(node.value)
            if inspect.ismodule(base) or inspect.isclass(base):
                return getattr(base, node.attr, _MISSING)
        return _MISSING


@functools.cache
def _scope(module_name: str, qualname: str) -> _Scope:
    return _Scope(module_name, _definitions(module_name)[qualname])


# A parameter's value at one call site: the argument expression, with the
# scope and the bindings it is evaluated in (a raise helper can pass its own
# parameter on to ``coded_detail``, so the caller's bindings ride along).
_Bindings = dict[str, tuple[ast.AST, _Scope, dict]]


def _bind(
    callee: ast.AST,
    callee_scope: _Scope,
    call: ast.Call,
    scope: _Scope,
    bound: _Bindings,
) -> _Bindings:
    """What each of ``callee``'s parameters is at ``call``, defaults included."""
    a = callee.args
    positional = [p.arg for p in (*a.posonlyargs, *a.args)]
    params: _Bindings = {}
    for name, default in zip(reversed(positional), reversed(a.defaults), strict=False):
        params[name] = (default, callee_scope, {})
    for p, default in zip(a.kwonlyargs, a.kw_defaults, strict=True):
        if default is not None:
            params[p.arg] = (default, callee_scope, {})
    for name, arg in zip(positional, call.args, strict=False):
        if isinstance(arg, ast.Starred):
            break
        params[name] = (arg, scope, bound)
    for kw in call.keywords:
        if kw.arg is not None:
            params[kw.arg] = (kw.value, scope, bound)
    return params


def _value(node: ast.AST, scope: _Scope, bound: _Bindings, depth: int = 0) -> object:
    """The runtime value an expression stands for, where the source fixes it."""
    if depth > 6:
        return _MISSING
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name) and node.id in bound:
        expr, where, outer = bound[node.id]
        return _value(expr, where, outer, depth + 1)
    return scope.resolve(node)


def _detail_code_of(
    node: ast.AST | None, scope: _Scope, bound: _Bindings, depth: int = 0
) -> object:
    """The ``code`` a detail expression carries: a string, ``None`` when it
    carries none, or ``_UNNAMED`` when it carries one the scan cannot name."""
    if node is None:
        return None
    if depth > 6:
        return _UNNAMED
    if isinstance(node, ast.Name) and node.id in bound:
        expr, where, outer = bound[node.id]
        return _detail_code_of(expr, where, outer, depth + 1)
    if isinstance(node, (ast.Constant, ast.JoinedStr, ast.BinOp)):
        return None
    if isinstance(node, ast.IfExp):
        branches = (node.body, node.orelse)
        either = {_detail_code_of(b, scope, bound, depth + 1) for b in branches}
        return either.pop() if len(either) == 1 else _UNNAMED
    if isinstance(node, ast.Dict):
        for key, value in zip(node.keys, node.values, strict=True):
            if isinstance(key, ast.Constant) and key.value == "code":
                code = _value(value, scope, bound, depth + 1)
                return code if isinstance(code, str) else _UNNAMED
        return None
    if isinstance(node, ast.Call):
        return _built_detail_code(node, scope, bound, depth)
    value = _value(node, scope, bound, depth + 1)
    if isinstance(value, str):
        return None
    if isinstance(value, dict):
        code = value.get("code")
        return code if isinstance(code, str) or code is None else _UNNAMED
    return _UNNAMED


def _built_detail_code(
    call: ast.Call, scope: _Scope, bound: _Bindings, depth: int
) -> object:
    """The code of a detail a function builds — ``coded_detail(code, …)``
    returns ``{"code": code, …}``, so its code is bound at each call."""
    if isinstance(call.func, ast.Attribute) and call.func.attr in _TEXT_METHODS:
        return None
    target = scope.resolve(call.func)
    if target is str:
        return None
    found = _definition(target, ("core_api", "common"))
    builder = _definitions(found[0])[found[1]] if found else None
    if not isinstance(builder, _DEFS):
        return _UNNAMED
    builder_scope = _scope(*found)
    params = _bind(builder, builder_scope, call, scope, bound)
    returned = [
        sub.value
        for sub in ast.walk(builder)
        if isinstance(sub, ast.Return) and sub.value is not None
    ]
    codes = {_detail_code_of(r, builder_scope, params, depth + 1) for r in returned}
    codes.discard(None)
    if len(codes) == 1:
        return codes.pop()
    return _UNNAMED if codes else None


def _raise_code(call: ast.Call, scope: _Scope, bound: _Bindings) -> str | None:
    """The code ``_detail_code`` gives this ``HTTPException``, or None."""
    status = call.args[0] if call.args else None
    detail = call.args[1] if len(call.args) > 1 else None
    for kw in call.keywords:
        if kw.arg == "status_code":
            status = kw.value
        elif kw.arg == "detail":
            detail = kw.value
    code = _detail_code_of(detail, scope, bound)
    if isinstance(code, str):
        return code
    if code is _UNNAMED or status is None:
        return None
    value = _value(status, scope, bound)
    if isinstance(value, int) and not isinstance(value, bool):
        return code_for_status(value)
    return None


@functools.cache
def _own_raises(
    module_name: str, qualname: str
) -> tuple[list[ast.Call], list[ast.Call]]:
    """A function's ``HTTPException`` calls: ``(fixed, riding on a parameter)``."""
    node = _definitions(module_name)[qualname]
    scope = _scope(module_name, qualname)
    params = set(_params(node))
    raises = [
        call
        for call in ast.walk(node)
        if isinstance(call, ast.Call) and _is_http_exception(scope.resolve(call.func))
    ]
    fixed: list[ast.Call] = []
    waiting: list[ast.Call] = []
    for call in raises:
        mentions = {sub.id for sub in ast.walk(call) if isinstance(sub, ast.Name)}
        if _raise_code(call, scope, {}) is None and mentions & params:
            waiting.append(call)
        else:
            fixed.append(call)
    return fixed, waiting


def _maps_http_errors(handler: ast.ExceptHandler) -> bool:
    """An ``except`` clause that answers what it caught with ``_detail_code``."""
    return any(
        isinstance(sub, ast.Call) and getattr(sub.func, "id", None) == "_detail_code"
        for sub in ast.walk(handler)
    )


def _guarded_calls(function: ast.AST) -> list[ast.Call]:
    """Calls inside a ``try`` whose ``except`` answers with ``_detail_code``."""
    calls = []
    for node in ast.walk(function):
        if isinstance(node, ast.Try) and any(map(_maps_http_errors, node.handlers)):
            for stmt in node.body:
                calls += [c for c in ast.walk(stmt) if isinstance(c, ast.Call)]
    return calls


def _reach(function_name: str) -> set[str]:
    """``function_name`` and every ``mcp_server`` helper it calls, transitively."""
    functions, _ = _module()
    seen: set[str] = set()
    stack = [function_name]
    while stack:
        name = stack.pop()
        if name in seen or name not in functions:
            continue
        seen.add(name)
        stack.extend(_calls(functions[name]))
    return seen


class _ServiceWalk:
    """Path 6 for one handler: the codes found, the raises it could not name
    (``module:line``), and the definitions it reached."""

    def __init__(self, function_name: str) -> None:
        from core_api import mcp_server

        self.codes: set[str] = set()
        self.unresolved: set[str] = set()
        self.reached: set[tuple[str, str]] = set()
        self._work: list[tuple[str, str]] = []
        # Raises whose code rides on a parameter: named at each call site the
        # walk sees, and reported if it never sees one.
        self._waiting: dict[tuple[str, str], list[ast.Call]] = {}
        self._bound: set[tuple[str, str]] = set()

        functions, _ = _module()
        for name in _reach(function_name):
            scope = _Scope(mcp_server.__name__, functions[name])
            for call in _guarded_calls(functions[name]):
                self._visit(call, scope)
        while self._work:
            self._walk(self._work.pop())
        for key, waiting in self._waiting.items():
            if key not in self._bound:
                self.unresolved.update(f"{key[0]}:{c.lineno}" for c in waiting)

    def _record(
        self, call: ast.Call, scope: _Scope, bound: _Bindings, site: str
    ) -> None:
        code = _raise_code(call, scope, bound)
        if code is None:
            self.unresolved.add(site)
        else:
            self.codes.add(code)

    def _visit(self, call: ast.Call, scope: _Scope) -> None:
        site = f"{scope.module_name}:{call.lineno}"
        target = scope.resolve(call.func)
        if _is_http_exception(target):
            self._record(call, scope, {}, site)
            return
        found = _definition(target)
        if found is None:
            return
        if inspect.isclass(target):
            # Calling a class runs its methods later, through the instance.
            self._work.extend(_methods(target))
            return
        self._work.append(found)
        callee, callee_scope = _definitions(found[0])[found[1]], _scope(*found)
        for raise_call in _own_raises(*found)[1]:
            self._bound.add(found)
            bound = _bind(callee, callee_scope, call, scope, {})
            self._record(raise_call, callee_scope, bound, site)

    def _walk(self, key: tuple[str, str]) -> None:
        if key in self.reached:
            return
        self.reached.add(key)
        scope = _scope(*key)
        fixed, waiting = _own_raises(*key)
        for call in fixed:
            self._record(call, scope, {}, f"{key[0]}:{call.lineno}")
        if waiting:
            self._waiting[key] = waiting
        own = {id(call) for call in fixed + waiting}
        for call in ast.walk(_definitions(key[0])[key[1]]):
            if isinstance(call, ast.Call) and id(call) not in own:
                self._visit(call, scope)


@functools.cache
def _service_walk(function_name: str) -> _ServiceWalk:
    return _ServiceWalk(function_name)


def service_codes(function_name: str) -> frozenset[str]:
    """Codes an ``HTTPException`` raised below ``function_name`` arrives as."""
    return frozenset(_service_walk(function_name).codes)


def unresolved_raises(function_name: str) -> frozenset[str]:
    """``module:line`` of each raise path 6 reaches but cannot name a code for."""
    return frozenset(_service_walk(function_name).unresolved)


def reached_definitions(function_name: str) -> frozenset[str]:
    """Dotted names of the definitions path 6 walked, for the scan's own guards."""
    return frozenset(f"{m}.{q}" for m, q in _service_walk(function_name).reached)


def tool_codes(function_name: str) -> frozenset[str]:
    """Every code a caller of this handler can receive: paths 1-6."""
    own = emitted_codes(function_name)
    return own | dispatch_codes() | service_codes(function_name)
