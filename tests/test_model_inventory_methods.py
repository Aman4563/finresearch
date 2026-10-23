"""#379 CI gate: every `method=` given to a Signal (or a forecast-ledger row) anywhere in src/finresearch maps to a
model-inventory entry. The scan reads the source: a method that is a constant, an f-string or a concatenation is
checked by its literal prefix; a local variable is followed to every value assigned to it (including the tuples a
for loop unpacks); a name or attribute outside the function is read from the imported module. Anything the scan
cannot follow must be listed in RESOLVED by hand, so a new method can never slip through unmapped."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

from finresearch.inventory import registry
from finresearch.signals.ipo import BLEND_METHOD

SRC = Path(__file__).resolve().parents[1] / "src" / "finresearch"
# signals.base.Signal, signals.fund's local `signal` helper, ledger rows, and dict(...) kwargs later splatted into one
CALLS = {"Signal", "signal", "Forecast", "dict"}
# (file, function) → the methods a site there can produce, for expressions the scan cannot follow
RESOLVED: dict[tuple[str, str], list[str]] = {
    ("signals/ipo.py", "shadow_signal"): [BLEND_METHOD],  # shadow["method"]: only _shadow_blend builds one
    ("signals/ledger.py", "record"): [],  # copies an already-checked Signal's method
    ("signals/ledger.py", "forecast_json"): [],  # reads a stored row back
    ("signals/ledger.py", "calibration_groups"): [],  # groups stored rows by their method
}
# a helper whose `method` parameter is resolved at its call sites (also scanned, as calls named in CALLS)
PARAM_HELPERS = {("signals/fund.py", "signal")}


class Unresolved(Exception):
    pass


def _module_name(path: Path) -> str:
    return "finresearch." + ".".join(path.relative_to(SRC).with_suffix("").parts)


def _assigned(fn: ast.AST, name: str) -> list[ast.expr]:
    """Every value bound to `name` inside `fn`: assignments, appended tuples' positions handled by the caller."""
    out: list[ast.expr] = []
    for n in ast.walk(fn):
        if (
            isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in n.targets)
        ) or (
            isinstance(n, ast.AnnAssign)
            and isinstance(n.target, ast.Name)
            and n.target.id == name
            and n.value
        ):
            out.append(n.value)
    return out


def _for_values(fn: ast.AST, name: str) -> list[ast.expr] | None:
    """The values a `for (..., name, ...) in <tuples>` loop binds to `name`, or None when no loop binds it."""
    for n in ast.walk(fn):
        if not (isinstance(n, ast.For) and isinstance(n.target, ast.Tuple)):
            continue
        idx = next((i for i, e in enumerate(n.target.elts) if isinstance(e, ast.Name) and e.id == name), None)
        if idx is None:
            continue
        seqs: list[ast.expr] = [n.iter]
        if isinstance(n.iter, ast.Name):  # a list built in the function: its literals and appended tuples
            seqs = _assigned(fn, n.iter.id)
            seqs += [c.args[0] for c in ast.walk(fn) if isinstance(c, ast.Call)
                     and isinstance(c.func, ast.Attribute) and c.func.attr == "append"
                     and isinstance(c.func.value, ast.Name) and c.func.value.id == n.iter.id]  # fmt: skip
        out = []
        for seq in seqs:
            items = seq.elts if isinstance(seq, ast.List | ast.Tuple) and not (
                seq.elts and not isinstance(seq.elts[0], ast.Tuple)) else [seq]  # fmt: skip
            for it in items:
                if not isinstance(it, ast.Tuple):
                    raise Unresolved(ast.unparse(it))
                out.append(it.elts[idx])
        return out
    return None


def _lookup(e: ast.expr, mod):
    """`NAME` or `alias.NAME` read from the module's namespace with getattr (never evaluated as code)."""
    if isinstance(e, ast.Name):
        if not hasattr(mod, e.id):
            raise Unresolved(e.id)
        return getattr(mod, e.id)
    if isinstance(e, ast.Attribute):
        base = _lookup(e.value, mod)
        if not hasattr(base, e.attr):
            raise Unresolved(ast.unparse(e))
        return getattr(base, e.attr)
    raise Unresolved(ast.unparse(e))


def _resolve(e: ast.expr, fns: list[ast.AST], mod) -> list[str]:
    if isinstance(e, ast.Constant) and isinstance(e.value, str):
        return [e.value]
    if isinstance(e, ast.JoinedStr):
        head = e.values[0] if e.values else None
        if isinstance(head, ast.Constant) and head.value.strip():
            return [head.value]
        raise Unresolved(ast.unparse(e))
    if isinstance(e, ast.BinOp) and isinstance(e.op, ast.Add):
        return _resolve(e.left, fns, mod)
    if isinstance(e, ast.BoolOp):
        return [m for v in e.values for m in _resolve(v, fns, mod)]
    if isinstance(e, ast.IfExp):
        return _resolve(e.body, fns, mod) + _resolve(e.orelse, fns, mod)
    if isinstance(e, ast.Subscript) and isinstance(e.slice, ast.Slice):  # method[:200]
        return _resolve(e.value, fns, mod)
    if isinstance(e, ast.Name):
        for f in fns:  # innermost enclosing function first (a nested helper reads its parent's local)
            vals = _assigned(f, e.id)
            loop = _for_values(f, e.id)
            if vals or loop:
                return [m for v in [*vals, *(loop or [])] for m in _resolve(v, fns, mod)]
            for imp in (
                n for n in ast.walk(f) if isinstance(n, ast.ImportFrom) and n.module
            ):  # a local import
                if any((a.asname or a.name) == e.id for a in imp.names):
                    orig = next(a.name for a in imp.names if (a.asname or a.name) == e.id)
                    v = getattr(importlib.import_module(imp.module), orig, None)
                    if isinstance(v, str):
                        return [v]
    if isinstance(e, ast.Name | ast.Attribute):
        v = _lookup(e, mod)  # a module constant or an imported module's attribute
        if isinstance(v, str):
            return [v]
    raise Unresolved(ast.unparse(e))


def _row_method(node: ast.AST) -> ast.expr | None:
    """A forecast-ledger row written as a dict literal ({"method": ..., "validation_status": ...}, later
    `insert(Forecast).values(**row)`): its method expression (#379: the VaR ledger logs its rows this way)."""
    if not isinstance(node, ast.Dict):
        return None
    keys = {k.value: v for k, v in zip(node.keys, node.values, strict=True) if isinstance(k, ast.Constant)}
    return keys["method"] if "method" in keys and "validation_status" in keys else None


def _sites():
    """(where, [method templates]) for every method= keyword of a call in CALLS, and every forecast-row dict."""
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        tree = ast.parse(path.read_text())
        mod = None
        funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]
        for call in ast.walk(tree):
            if (row := _row_method(call)) is not None:
                kw = ast.keyword("method", row)
            elif isinstance(call, ast.Call):
                name = call.func.id if isinstance(call.func, ast.Name) else getattr(call.func, "attr", None)
                kw = next((k for k in call.keywords if k.arg == "method"), None)
                if name not in CALLS or kw is None:
                    continue
            else:
                continue
            fns = sorted((f for f in funcs if f.lineno <= call.lineno <= (f.end_lineno or 0)),
                         key=lambda f: (f.end_lineno or 0) - f.lineno)  # fmt: skip
            fname = fns[0].name if fns else "<module>"
            if (rel, fname) in RESOLVED:
                yield f"{rel}:{call.lineno}", RESOLVED[(rel, fname)], True
                continue
            if (rel, fname) in PARAM_HELPERS and isinstance(kw.value, ast.BoolOp):
                kw = ast.keyword("method", kw.value.values[-1])  # the default; callers pass the rest
            mod = mod or importlib.import_module(_module_name(path))
            try:
                yield f"{rel}:{call.lineno}", _resolve(kw.value, fns, mod), False
            except Unresolved as x:
                yield f"{rel}:{call.lineno}", [f"<unresolved: {x}>"], False


SITES = list(_sites())


def test_the_scan_finds_the_known_signal_producers():
    files = {w.split(":")[0] for w, _, _ in SITES}
    for f in ("signals/ipo.py", "signals/stock.py", "signals/fund.py", "signals/bond.py", "signals/fno.py",
              "direction/live.py", "fnobook/card_forecasts.py", "signals/ledger.py",
              "portfolio/var_ledger.py"):  # fmt: skip
        assert f in files, f


@pytest.mark.parametrize(("where", "methods", "by_hand"), SITES, ids=[w for w, _, _ in SITES])
def test_every_signal_method_maps_to_an_inventory_entry(where, methods, by_hand):
    for m in methods:
        assert not m.startswith("<unresolved"), f"{where}: {m}: list it in RESOLVED or make it a constant"
        assert registry.entry_for_method(m) is not None, f"{where}: method {m!r} has no model-inventory entry"


def test_the_scan_follows_locals_and_loops_and_flags_what_it_cannot():
    import types

    src = ("def f(x):\n    a = 'zzz unregistered ' + x\n    for k, m in [('a', 'loop method'), ('b', K)]:\n"
           "        Signal(method=m)\n    Signal(method=a)\n    Signal(method=x.y())\n")  # fmt: skip
    tree = ast.parse(src)
    fn = tree.body[0]
    mod = types.SimpleNamespace(K="module constant")
    calls = sorted((c for c in ast.walk(tree) if isinstance(c, ast.Call) and getattr(c.func, "id", "") == "Signal"),
                   key=lambda c: c.lineno)  # fmt: skip
    got = [_resolve(c.keywords[0].value, [fn], mod) if i < 2 else None for i, c in enumerate(calls)]
    assert got[0] == ["loop method", "module constant"] and got[1] == ["zzz unregistered "]
    assert registry.entry_for_method(got[1][0]) is None  # an unregistered method fails the gate
    with pytest.raises(Unresolved):
        _resolve(calls[2].keywords[0].value, [fn], mod)


# ---------------------------------------------------------------------------------------- registrations (#379 slice 2)
REG_NAMES = {"SPEC_VERSION", "ADDENDUM_VERSION"}
METHOD_NAMES = {"METHOD", "METHODS"}


def _module_constants(names: set[str]):
    """(file, name, value) for every module-level string (or tuple of strings) assignment named in `names`."""
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel.startswith("inventory/"):
            continue
        for n in ast.parse(path.read_text()).body:
            tgt = n.targets[0] if isinstance(n, ast.Assign) else n.target if isinstance(n, ast.AnnAssign) else None
            if isinstance(tgt, ast.Name) and tgt.id in names:
                v = getattr(importlib.import_module(_module_name(path)), tgt.id)
                for x in (v,) if isinstance(v, str) else v if isinstance(v, tuple) else ():
                    if isinstance(x, str):
                        yield rel, tgt.id, x


REGS = list(_module_constants(REG_NAMES))
CONSTS = list(_module_constants(METHOD_NAMES))


def test_the_registration_scan_finds_the_known_registrations():
    found = {v for _, _, v in REGS}
    assert {"vol-forecast-race-v1", "vrp-collectable-v1", "book-beta-v1", "card-horse-race-v1-addendum-A6"} <= found


@pytest.mark.parametrize(("where", "name", "value"), REGS, ids=[f"{w}:{v}" for w, _, v in REGS])
def test_every_registered_spec_version_maps_to_an_inventory_entry(where, name, value):
    assert registry.entry_for_registration(value) is not None, f"{where} {name}={value!r} has no inventory entry"


def test_every_horse_race_forecaster_and_challenger_maps_to_an_inventory_entry():
    from finresearch.horserace import addendum_a6, specs

    for key in (*specs.FORECASTERS, *addendum_a6.CHALLENGERS):
        assert registry.entry_for_registration(f"horserace:{key}") is not None, key


@pytest.mark.parametrize(("where", "name", "value"), CONSTS, ids=[f"{w}:{n}:{v[:20]}" for w, n, v in CONSTS])
def test_every_module_method_constant_maps_to_an_inventory_entry(where, name, value):
    """A model a page shows by its METHOD text (VaR, joint risk, factor exposures, Brinson, liquidity, the optimiser's
    methods, ...) must be in the inventory, so its status badge and limits of use are on the models page. A METHODS
    tuple's short names are registrations `<module>:<name>` (a bare "hrp" would be too loose as a method prefix)."""
    if name == "METHODS":
        key = f"{Path(where).stem}:{value}"
        assert registry.entry_for_registration(key) is not None, f"{where} {key!r} has no inventory entry"
    else:
        assert registry.entry_for_method(value) is not None, f"{where} {name}={value[:60]!r} has no inventory entry"
