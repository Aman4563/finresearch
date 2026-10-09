"""Claim ledger operations + deterministic citation checks (no LLM involved)."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.orm import Session

from finresearch.db.models import Citation, Claim, Document, ResearchRun
from finresearch.ingest.text import read_lines

CLAIM_TYPES = {"numeric", "factual", "opinion"}
IMPORTANCE = {"high", "normal", "low"}
DEVANAGARI = re.compile(r"[\u0900-\u097F]")
TRANSLATION_MARK = re.compile(r"translat|अनुवाद", re.I)
LINE_TOLERANCE = 2  # allow the quote to sit up to 2 lines outside the cited span (wrapped lines)


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).replace("\f", " ")
    s = (
        s.replace("’", "'")
        .replace("‘", "'")
        .replace("“", '"')
        .replace("”", '"')
        .replace("–", "-")
        .replace("—", "-")
    )
    return re.sub(r"\s+", " ", s).strip().lower()


def _loose(s: str) -> str:
    """Whitespace-, comma- and currency-insensitive form (tables re-flow; '4,891.56' vs '4891.56')."""
    return re.sub(r"[\s,₹]", "", _norm(s))


def quote_in_lines(doc: Document, line_start: int, line_end: int, quote: str) -> tuple[bool, str]:
    """True if `quote` occurs within [line_start-2, line_end+2] of the document's canonical text."""
    lines = read_lines(doc.text_path)
    if not (1 <= line_start <= line_end <= len(lines)):
        return False, f"line range {line_start}-{line_end} outside document (1-{len(lines)})"
    a, b = max(1, line_start - LINE_TOLERANCE), min(len(lines), line_end + LINE_TOLERANCE)
    window = "\n".join(lines[a - 1 : b])
    if _norm(quote) in _norm(window):
        return True, "exact"
    if _loose(quote) and _loose(quote) in _loose(window):
        return True, "loose (whitespace/commas)"
    # table rows: every number in the quote present in the window as a whole number (not a substring: "12" must not
    # match inside "1,234", nor "2.5" inside "12.5")
    nums = _num_tokens(quote)
    # the window is also read with "1, 234" rejoined (pdftotext can split a grouped number after its comma)
    in_window = set(_num_tokens(window)) | set(_num_tokens(re.sub(r"(?<=\d),\s+(?=\d)", ",", window)))
    if len(nums) >= 2 and set(nums) <= in_window:
        # a figure quoted as negative ("-512.40", "(512.40)") must be printed negative there too (issue #217)
        if _negatives(quote) <= _negatives(window) | _negatives(re.sub(r"(?<=\d),\s+(?=\d)", ",", window)):
            return True, "all quoted numbers present"
        return False, "a number quoted as negative is not negative in the cited lines"
    return False, "quote not found in cited lines"


def quote_in_text(quote: str, text: str) -> tuple[bool, str]:
    """True if `quote` occurs in a stored page's text, exactly or ignoring whitespace, commas and ₹ (#242). Unlike a
    document window there is no numbers-only fallback: a page is long, so loose number matches would prove nothing."""
    if not quote:
        return False, "no quote given"
    if _norm(quote) in _norm(text):
        return True, "exact (stored page)"
    if _loose(quote) and _loose(quote) in _loose(text):
        return True, "loose (whitespace/commas, stored page)"
    return False, "quote not found on the stored page"


def _web_citation(
    session: Session, run_id: int, url: str, quote: str, at: datetime
) -> tuple[Citation, dict[str, Any]]:
    """A web citation, checked against the latest stored snapshot of the page (verify.web) when there is one."""
    from finresearch.verify import web

    if not re.match(r"https?://", url, re.I):
        raise ValueError(f"a url citation must be an http(s) URL, got {url[:80]!r}; for a computed figure cite "
                         '{"fincalc": {"function": ..., "args": {...}}, "inputs": [claim ids]}')  # fmt: skip
    snap = web.latest(session, url, run_id)
    if snap is None:
        return (Citation(url=url, quote=quote or None, accessed_at=at),
                {"url": url, "quote_found": None,
                 "match": "unchecked web quote (grade C): fetch the page with fetch_page, then save the claim"})  # fmt: skip
    found, how = quote_in_text(quote, snap.text)
    return (Citation(url=url, quote=quote or None, accessed_at=at, quote_found=found, snapshot_sha256=snap.sha256),
            {"url": url, "quote_found": found, "match": how, "snapshot_sha256": snap.sha256})  # fmt: skip


def _close(stated: Decimal, result: Decimal) -> bool:
    """`stated` equals `result` rounded to the stated precision (half a unit of its last decimal), or within 0.05 %."""
    exp = stated.as_tuple().exponent
    half = Decimal(5) * Decimal(10) ** (exp - 1) if isinstance(exp, int) else Decimal(0)
    return abs(stated - result) <= max(half, abs(result) * Decimal("0.0005"))


_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
# fincalc arguments documented as positive magnitudes (ratios.fcf: "capex is the (positive) purchase of PP&E";
# EBIT/EBITDA add back finance cost and depreciation), while the cash-flow statement / P&L claim may carry the sign
_MAGNITUDE_ARGS = frozenset({"capex", "finance_cost", "depreciation"})


def _numeric_leaves(args: Any, path: str = "") -> list[tuple[str, Decimal]]:
    """Every numeric argument of a fincalc call with its path ("old", "quarters[2]", "bids.qib"); dates, bools and
    words ("floor", "crore") are not figures."""
    if isinstance(args, dict):
        return [x for k, v in args.items() for x in _numeric_leaves(v, f"{path}.{k}" if path else str(k))]
    if isinstance(args, list | tuple):
        return [x for i, v in enumerate(args) for x in _numeric_leaves(v, f"{path}[{i}]")]
    if (
        isinstance(args, bool)
        or args is None
        or (isinstance(args, str) and _ISO_DATE.fullmatch(args.strip()))
    ):
        return []
    if isinstance(args, int | float | Decimal | str):
        try:
            return [(path, Decimal(str(args).replace(",", "").strip()))]
        except InvalidOperation:
            return []
    return []


def _arg_matches(arg: Decimal, form: Decimal) -> bool:
    """`arg` equals a cited value within REL_TOL (0.05 % of the larger, the gate's rounding tolerance); zero only
    matches zero."""
    from finresearch.verify.gate import REL_TOL

    if not arg.is_finite():  # "NaN" / "Infinity" parse as Decimal but no claim holds them: never bound
        return False
    if arg == form:
        return True
    d = max(abs(arg), abs(form))
    return d != 0 and abs(arg - form) / d <= REL_TOL


def bind_args(args: dict[str, Any], claims: list[Claim], constants: dict[str, str]) -> dict[str, Any]:
    """Bind each numeric argument of a fincalc call to a distinct cited input claim whose value matches it, in the
    claim's own unit or any ₹ scale (crore claim -> rupee argument) or %/fraction (verify.gate.candidate_forms),
    within REL_TOL; or to a constant the citation declares with a reason (#265). Sign is kept, except for the
    magnitude arguments in _MAGNITUDE_ARGS. One claim binds one argument, so pct_change(old=200, new=200) cannot
    lean twice on the 200 claim. Returns {"bound": {path: claim id | "constant"}, "unbound": [paths], "ok": bool}."""
    from finresearch.verify.gate import candidate_forms

    leaves = _numeric_leaves(args)
    forms = {c.id: [f for f in candidate_forms(c.value, c.unit) if (f < 0) == (c.value < 0) or f == 0]
             for c in claims if c.value is not None}  # fmt: skip

    def fits(path: str, a: Decimal, cid: int) -> bool:
        mag = path.rsplit(".", 1)[-1].split("[")[0] in _MAGNITUDE_ARGS
        return any(_arg_matches(abs(a) if mag else a, abs(f) if mag else f) for f in forms[cid])

    cand = {p: [cid for cid in forms if fits(p, a, cid)] for p, a in leaves}
    owner: dict[int, str] = {}  # claim id -> argument path (maximum bipartite matching, augmenting paths)

    def assign(p: str, seen: set[int]) -> bool:
        for cid in cand[p]:
            if cid not in seen:
                seen.add(cid)
                if cid not in owner or assign(owner[cid], seen):
                    owner[cid] = p
                    return True
        return False

    for p, _ in leaves:
        assign(p, set())
    bound: dict[str, Any] = {p: cid for cid, p in owner.items()}
    for p, _ in leaves:
        top = p.split(".")[0].split("[")[0]
        if p not in bound and (constants.get(p) or constants.get(top) or "").strip():
            bound[p] = "constant"
    unbound = [p for p, _ in leaves if p not in bound]
    return {"bound": bound, "unbound": unbound,
            "ok": not unbound and any(v != "constant" for v in bound.values())}  # fmt: skip


def _computed_citation(session: Session, run_id: int, c: dict[str, Any], value: Decimal | None, unit: str | None,
                       fincalc: Callable[[str, dict[str, Any]], Any] | None) -> tuple[Citation, dict[str, Any]]:  # fmt: skip
    """A figure computed by fincalc: the call is executed again and must reproduce the claim's value, and its inputs
    must be claims of this run that are not unsupported or contradicted (grade D, #242), and every numeric argument
    must be one of those inputs' values or a declared constant (bind_args, #265): a correct calculation on figures
    nobody cited is not evidence."""
    import json

    from sqlalchemy import select

    spec, inputs = c.get("fincalc"), c.get("inputs")
    if not isinstance(spec, dict) or not isinstance(spec.get("function"), str) or not isinstance(
            spec.get("args") or {}, dict):  # fmt: skip
        raise ValueError('a fincalc citation is {"fincalc": {"function": "growth.cagr", "args": {...}}, '
                         '"inputs": [claim ids of the inputs]}')  # fmt: skip
    try:
        ids = [int(x) for x in inputs or []]
    except (TypeError, ValueError) as e:
        raise ValueError(f"fincalc inputs must be claim ids, got {inputs!r}") from e
    constants = c.get("constants") or {}
    if not isinstance(constants, dict) or not all(isinstance(k, str) and isinstance(v, str) and v.strip()
                                                  for k, v in constants.items()):  # fmt: skip
        raise ValueError('fincalc "constants" is {"<argument>": "<why this value, with its source>"} for arguments '
                         'that are not a cited claim, e.g. {"years": "FY2023 to FY2026 is 3 fiscal years"}')  # fmt: skip
    fn, args, key = spec["function"], spec.get("args") or {}, spec.get("result_key")
    if fincalc is None:
        raise ValueError("fincalc citations are checked by the MCP server's save_claim")
    try:
        result = fincalc(fn, args)
    except (ValueError, TypeError, ArithmeticError) as e:
        raise ValueError(f"the fincalc citation does not run: {type(e).__name__}: {e}") from e
    if isinstance(result, dict):
        if key not in result:
            raise ValueError(
                f"{fn} returns several values {sorted(result)[:12]}: name the one cited as result_key"
            )
        result = result[key]
    rd = (
        _dec(result)
        if isinstance(result, int | float | str | Decimal) and not isinstance(result, bool)
        else None
    )
    pct = "%" in (unit or "") or "percent" in (unit or "").lower()
    matches = (
        value is not None and rd is not None and (_close(value, rd) or (pct and _close(value, rd * 100)))
    )
    rows = {x.id: x for x in session.scalars(select(Claim).where(Claim.id.in_(ids)))} if ids else {}
    bad = [i for i in ids if i not in rows or rows[i].run_id != run_id
           or rows[i].status in ("unsupported", "contradicted")]  # fmt: skip
    inputs_ok = bool(ids) and not bad
    binding = bind_args(args, [rows[i] for i in ids if i in rows and i not in bad], constants)
    detail = (
        "fincalc reproduces the value" if matches else f"fincalc gives {result}, not the stated {value}"
    ) + ("; inputs cited" if inputs_ok else f"; inputs not usable ({'none cited' if not ids else bad})")
    if binding["ok"]:
        detail += "; every argument is a cited input's value (within 0.05%) or a declared constant"
    elif binding["unbound"]:
        seen = ", ".join(f"[C{i}] {rows[i].value.normalize():f} {rows[i].unit or ''}".strip()
                         for i in ids if i in rows and rows[i].value is not None) or "none"  # fmt: skip
        detail += (f"; argument(s) {', '.join(binding['unbound'])} match no cited input claim (within 0.05%, any ₹ "
                   f"scale or %/fraction; each claim binds one argument) and are not declared constants; cited "
                   f"values: {seen}")  # fmt: skip
    else:
        detail += "; no argument is bound to a cited input claim (constants only)"
    url = f"fincalc:{fn}({json.dumps(args, sort_keys=True, default=str)})"[:2000]
    comp = {"function": fn, "args": args, "result_key": key, "result": str(result), "inputs": ids,
            "matches": bool(matches), "inputs_ok": inputs_ok, "constants": constants or None,
            "bindings": {k: binding["bound"][k] for k in sorted(binding["bound"])},
            "args_bound": binding["ok"], "detail": detail}  # fmt: skip
    found = bool(matches) and inputs_ok and binding["ok"]
    return (Citation(url=url, quote=(c.get("quote") or f"{fn} = {result}")[:1000], quote_found=found,
                     computation=comp, accessed_at=datetime.now(UTC)),
            {"fincalc": fn, "quote_found": found, "match": detail})  # fmt: skip


def _negatives(s: str) -> set[Decimal]:
    from finresearch.verify.values import has_value_like, tokens

    # figures only: a footnote marker " (1) " is not a negative one
    return {
        abs(t.value)
        for ln in s.split("\n")
        for t in tokens(ln)
        if t.neg and (abs(t.value) >= 10 or has_value_like(ln[t.start : t.end]))
    }


_NUM_TOKEN = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _num_tokens(s: str) -> list[str]:
    """Whole numbers in `s` with their grouping commas dropped ("1,234.5" -> "1234.5", "12,34,567" -> "1234567")."""
    return [m.group(0).rstrip(",").replace(",", "") for m in _NUM_TOKEN.finditer(_norm(s))]


def _dec(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v).replace(",", ""))
    except InvalidOperation as e:
        raise ValueError(f"value must be numeric, got {v!r}") from e


def save_claim(
    session: Session,
    *,
    run_id: int,
    stream: str,
    statement: str,
    claim_type: str,
    citations: list[dict[str, Any]],
    metric: str | None = None,
    value: Any = None,
    unit: str | None = None,
    period: str | None = None,
    importance: str = "normal",
    fincalc: Callable[[str, dict[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """Validate and store one claim with its citations (see the save_claim MCP tool). `fincalc` runs a computed
    figure's fincalc citation again (mcp_server.server.run_fincalc)."""
    if claim_type not in CLAIM_TYPES:
        raise ValueError(f"claim_type must be one of {sorted(CLAIM_TYPES)}")
    if importance not in IMPORTANCE:
        raise ValueError(f"importance must be one of {sorted(IMPORTANCE)}")
    if session.get(ResearchRun, run_id) is None:
        raise ValueError(f"unknown run_id {run_id}")
    if claim_type != "opinion" and not citations:
        raise ValueError("numeric/factual claims need at least one citation")
    if claim_type == "numeric":
        missing = [
            k
            for k, v in (("metric", metric), ("value", value), ("unit", unit), ("period", period))
            if v in (None, "")
        ]
        if missing:
            raise ValueError(
                f"numeric claims are atomic: one figure with metric, value, unit and period (missing {missing}). "
                "Save each figure of a table as its own claim."
            )

    # every citation is checked before anything is written: a bad one must not leave a half-saved claim behind
    # (the agent fixes the citation and saves again, which would duplicate it)
    parsed: list[tuple[Citation, dict[str, Any]]] = []
    for c in citations:
        if c.get("document_id"):
            try:
                doc_id = int(c["document_id"])
                ls = int(c["line_start"])
                le = int(c.get("line_end") or ls)
            except (KeyError, TypeError, ValueError) as e:
                raise ValueError(
                    f"a document citation needs integer document_id and line_start: {c!r}"
                ) from e
            doc = session.get(Document, doc_id)
            if doc is None:
                raise ValueError(f"unknown document_id {c['document_id']}")
            quote = (c.get("quote") or "").strip()
            found, how = quote_in_lines(doc, ls, le, quote) if quote else (False, "no quote given")
            page = _page_of(session, doc.id, ls)
            parsed.append((Citation(document_id=doc.id, page_no=page, line_start=ls, line_end=le, quote=quote,
                                    quote_found=found),
                           {"document_id": doc.id, "lines": f"{ls}-{le}", "page": page, "quote_found": found,
                            "match": how}))  # fmt: skip
        elif c.get("fincalc"):
            parsed.append(_computed_citation(session, run_id, c, _dec(value), unit, fincalc))
        elif c.get("url"):
            accessed = c.get("accessed_at")
            try:
                at = datetime.fromisoformat(accessed) if accessed else datetime.now(UTC)
            except (TypeError, ValueError) as e:
                raise ValueError(f"accessed_at must be an ISO date-time, got {accessed!r}") from e
            parsed.append(_web_citation(session, run_id, c["url"], (c.get("quote") or "").strip(), at))
        else:
            raise ValueError("each citation needs document_id+line_start(+line_end)+quote, url(+quote), or "
                             "fincalc+inputs")  # fmt: skip

    claim = Claim(
        run_id=run_id,
        stream=stream,
        statement=statement,
        claim_type=claim_type,
        metric=metric,
        value=_dec(value),
        unit=unit,
        period=period,
        importance=importance,
    )
    claim.citations = [cit for cit, _ in parsed]
    session.add(claim)
    session.flush()
    checks = [chk for _, chk in parsed]
    doc_checks = [x for x in checks if "document_id" in x]
    checked = [x for x in checks if x["quote_found"] is not None]
    if doc_checks and not any(x["quote_found"] for x in doc_checks):
        claim.status = "unsupported"
        claim.verifier_note = "no cited quote was found at the cited lines"
    elif checked and len(checked) == len(checks) and not any(x["quote_found"] for x in checked):
        # every citation was checked (web quote against its stored page, fincalc re-run) and none held (#242)
        claim.status = "unsupported"
        claim.verifier_note = (
            "no citation could be confirmed: " + "; ".join(str(x.get("match")) for x in checked)
        )[:1500]
    out: dict[str, Any] = {"claim_id": claim.id, "status": claim.status, "citation_checks": checks}
    if any(DEVANAGARI.search(c.get("quote") or "") for c in citations):
        # live run 7: the agent kept the Hindi quote but did not mark its English statement as a translation, even
        # when told to, so the marker is added deterministically
        auto = not TRANSLATION_MARK.search(statement)
        if auto:
            claim.statement = f"{statement.rstrip()} (translated from Hindi)"
            out["note"] = (
                "Hindi source: the statement was marked '(translated from Hindi)'; keep the quote in Hindi."
            )
        claim.checks = {**(claim.checks or {}), "source_language": "hi", "translation_marked": True,
                        "translation_mark_added": auto}  # fmt: skip
    session.flush()
    return out


def _page_of(session: Session, document_id: int, line: int) -> int | None:
    from sqlalchemy import select

    from finresearch.db.models import DocumentPage

    return session.scalar(
        select(DocumentPage.page_no).where(
            DocumentPage.document_id == document_id,
            DocumentPage.line_start <= line,
            DocumentPage.line_end >= line,
        )
    )
