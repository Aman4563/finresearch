"""Importers: CAMS/KFintech consolidated account statements (CAS) and broker equity tradebooks -> normalised rows.

Nothing here touches the database or the network, and nothing is logged: statements are personal data.

* CAS PDFs are parsed by the MIT-licensed `casparser` (https://github.com/codereverser/casparser, 1.x, pypdfium2
  backend). The PDF password is an argument only: it is never stored, logged or echoed back. Identity fields that
  casparser returns (investor name, PAN, email, address, mobile) are dropped here; only the AMC + folio label is kept.
  NSDL/CDSL depository statements have holdings but no transactions, so they are read for reconciliation only.
* Tradebooks are detected by their header row (CSV or XLSX; a preamble above the header is skipped):
  - Zerodha Console: ``symbol,isin,trade_date,exchange,segment,series,trade_type,auction,quantity,price,trade_id,
    order_id,order_execution_time`` (checked against a real export fixture in codereverser/folioman and an
    independent beancount importer; research notes 30-Sep-2026).
  - Groww stock order history: ``Stock name, Symbol, ISIN, Type, Quantity, Value, Exchange, Exchange Order Id,
    Execution date and time, Order status`` (from open-source importers, not a raw file). ``Value`` is the order
    total, so price = Value / Quantity; only ``Executed`` rows count.
  - Upstox trade book: ``Date, Company, Amount, Exchange, Segment, Scrip Code, Instrument Type, Strike Price, Expiry,
    Trade Num, Trade Time, Side, Quantity, Price`` — column names from open-source importers, exact spelling and order
    unverified [U]. ``Company`` is a name, not a symbol: such holdings need their NSE symbol set by hand to be priced.
  Tradebooks carry no charges, bonus/split shares, IPO allotments or off-market transfers: add those manually (or sync
  corporate actions), and edit charges if you want them in the cost.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")


class StatementError(ValueError):
    """A file that cannot be imported, with a message safe to show (never contains the password)."""


@dataclass
class ImportedTxn:
    account: str
    asset_type: str  # stock | mf | other
    name: str
    day: date
    kind: str  # buy | sell | dividend | opening | remove
    quantity: Decimal | None = None
    price: Decimal | None = None
    amount: Decimal | None = None
    charges: Decimal = Decimal(0)
    stt_paid: bool = True
    source: str = "manual"
    isin: str | None = None
    nse_symbol: str | None = None
    bse_code: str | None = None
    scheme_code: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    ext: str = ""  # source-specific identity used for de-duplication (trade id, CAS row text)

    @property
    def ikey(self) -> str:
        return instrument_key(
            self.asset_type, self.isin, self.nse_symbol, self.bse_code, self.scheme_code, self.name
        )

    def dedupe_key(self, occurrence: int = 0) -> str:
        parts = [self.source, self.account, self.ikey, self.day.isoformat(), self.kind, _s(self.quantity),
                 _s(self.amount), self.ext, str(occurrence)]  # fmt: skip
        return hashlib.sha256("|".join(parts).encode()).hexdigest()


@dataclass
class ClosingBalance:
    """What the statement says is held at its end: the reconciliation target for the lots."""

    ikey: str
    account: str
    name: str
    units: Decimal
    nav: Decimal | None = None
    value: Decimal | None = None
    day: date | None = None
    cost: Decimal | None = None


@dataclass
class ImportResult:
    kind: str  # cas | tradebook
    source: str  # CAMS | KFINTECH | NSDL | CDSL | zerodha | groww | upstox
    txns: list[ImportedTxn]
    closing: list[ClosingBalance] = field(default_factory=list)
    period: tuple[str, str] | None = None
    warnings: list[str] = field(default_factory=list)
    skipped: Counter = field(default_factory=Counter)
    holdings_only: bool = False  # a depository CAS: holdings for reconciliation, no transactions to import


def instrument_key(asset_type: str, isin: str | None, nse_symbol: str | None, bse_code: str | None,
                   scheme_code: str | None, name: str) -> str:  # fmt: skip
    if isin and ISIN_RE.match(isin):
        return f"ISIN:{isin}"
    if scheme_code:
        return f"MF:{scheme_code}"
    if nse_symbol:
        return f"NSE:{nse_symbol.upper()}"
    if bse_code:
        return f"BSE:{bse_code}"
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:50]
    return f"NAME:{asset_type}:{slug}"


def assign_dedupe_keys(txns: list[ImportedTxn]) -> list[str]:
    """Identical rows inside one file (two SIPs of the same amount on the same day) get occurrence 0, 1, ... so they
    stay distinct, while the same rows in an overlapping statement map to the same keys (idempotent re-import)."""
    seen: Counter = Counter()
    out = []
    for t in txns:
        base = t.dedupe_key(0)
        out.append(t.dedupe_key(seen[base]))
        seen[base] += 1
    return out


def _s(v: Decimal | None) -> str:
    return "" if v is None else format(v.normalize(), "f")


def _dec(v: Any) -> Decimal | None:
    if v is None:
        return None
    if isinstance(v, Decimal):
        return v
    if isinstance(v, int | float):
        return Decimal(str(v))
    t = str(v).strip().replace(",", "").replace("₹", "").replace("Rs.", "").strip()
    if t in ("", "-", "--", "NA", "N/A"):
        return None
    neg = t.startswith("(") and t.endswith(")")
    try:
        d = Decimal(t.strip("()"))
    except InvalidOperation:
        return None
    return -d if neg else d


_DATE_FORMATS = (
    "%Y-%m-%d",
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%d-%b-%Y",
    "%d %b %Y",
    "%Y/%m/%d",
    "%d-%m-%y",
    "%d/%m/%y",
    "%b %d, %Y",
)


def infer_month_first(values: list[Any]) -> bool:
    """Whether a file's numeric dates are month-first. Indian brokers write day-first, but a file opened and re-saved
    in a US locale is month-first, and a single date like 03-04-2025 cannot tell. The whole column decides: a first
    field above 12 proves day-first, a second field above 12 proves month-first; both in one file is an error, never
    a silent mix of 3-Apr and 4-Mar."""
    day_first = month_first = False
    for v in values:
        if isinstance(v, date):
            continue
        m = _NUMERIC_DATE.match(_date_text(v))
        if not m:
            continue
        a, b = int(m.group(1)), int(m.group(2))
        day_first |= a > 12
        month_first |= b > 12
    if day_first and month_first:
        raise StatementError("the file mixes day-first and month-first dates (e.g. 13-01 and 01-13): fix the date "
                             "column to one format and import again")  # fmt: skip
    return month_first


_MONTH_FIRST = {
    "%d-%m-%Y": "%m-%d-%Y",
    "%d/%m/%Y": "%m/%d/%Y",
    "%d-%m-%y": "%m-%d-%y",
    "%d/%m/%y": "%m/%d/%y",
}
_NUMERIC_DATE = re.compile(r"^(\d{1,2})[-/](\d{1,2})[-/](\d{2}|\d{4})$")


def _date_text(v: Any) -> str:
    t = str(v or "").strip()
    return re.split(r"[T ]\d{1,2}:\d{2}", t)[0].strip().rstrip(",")


def parse_day(v: Any, month_first: bool = False) -> date | None:
    """A trade date from a tradebook cell: ISO, DD-MM-YYYY, DD/MM/YYYY, 12-Jan-2025, or with a time appended.
    `month_first` reads numeric dates as MM-DD-YYYY (a file re-saved in a US locale; see `infer_month_first`)."""
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    t = _date_text(v)
    if not t:
        return None
    formats = [_MONTH_FIRST.get(f, f) for f in _DATE_FORMATS] if month_first else _DATE_FORMATS
    for fmt in formats:
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------- CAS (casparser)
BUY_TYPES = {"PURCHASE", "PURCHASE_SIP", "SWITCH_IN"}
SELL_TYPES = {"REDEMPTION", "SWITCH_OUT"}
CHARGE_TYPES = {"STT_TAX", "STAMP_DUTY_TAX"}


def _cas_day(v: Any) -> date | None:
    if isinstance(v, date):
        return v
    return parse_day(v)


def parse_cas(pdf: bytes, password: str) -> ImportResult:
    """Parse a CAS PDF with casparser. Raises StatementError with a safe message (wrong password, not a CAS ...)."""
    try:
        import casparser
        from casparser.exceptions import CASParseError, IncorrectPasswordError
    except ModuleNotFoundError as e:  # pragma: no cover - a declared dependency
        raise StatementError("casparser is not installed (uv sync)") from e
    try:
        data = casparser.read_cas_pdf(io.BytesIO(pdf), password)
    except IncorrectPasswordError as e:
        raise StatementError("wrong password for this PDF (CAS PDFs are usually locked with your PAN)") from e
    except CASParseError as e:
        raise StatementError(f"casparser could not read this file as a CAS: {type(e).__name__}") from e
    except Exception as e:  # a malformed PDF: never include the exception text (it could quote inputs)
        raise StatementError(f"could not read this PDF ({type(e).__name__})") from e
    file_type = str(getattr(data, "file_type", "UNKNOWN"))
    if file_type in ("NSDL", "CDSL"):
        return _demat_holdings(data, file_type)
    return cas_to_result(data.model_dump(mode="json", by_alias=True))


def cas_to_result(d: dict[str, Any]) -> ImportResult:
    """casparser's CAMS/KFintech CASData (as JSON) -> transactions + closing balances. Identity fields are ignored."""
    source = str(d.get("file_type") or "CAS")
    if str(d.get("cas_type")) == "SUMMARY":
        raise StatementError(
            "this is a CAS summary (holdings only): download the detailed CAS with transactions"
        )
    period = d.get("statement_period") or {}
    start = parse_day(period.get("from") or period.get("from_"))
    res = ImportResult("cas", source, [], period=(str(period.get("from") or period.get("from_") or ""),
                                                   str(period.get("to") or "")))  # fmt: skip
    res.warnings += [str(w) for w in d.get("parse_warnings") or []]
    for folio in d.get("folios") or []:
        account = f"{folio.get('amc') or 'MF'} · folio {folio.get('folio')}"[:80]
        for sc in folio.get("schemes") or []:
            name = str(sc.get("scheme") or "Unknown scheme")
            common = dict(account=account, asset_type="mf", name=name, isin=sc.get("isin") or None,
                          scheme_code=(str(sc["amfi"]) if sc.get("amfi") else None), source="cas")  # fmt: skip
            base_meta = {"rta": sc.get("rta"), "rta_code": sc.get("rta_code"), "cas_type": sc.get("type")}
            opening = _dec(sc.get("open")) or Decimal(0)
            if opening > 0:
                if start is None:
                    res.warnings.append(f"{name}: opening balance without a statement start date skipped")
                else:
                    res.txns.append(ImportedTxn(**common, day=start, kind="opening", quantity=opening,
                                                meta={**base_meta, "statement_opening": True,
                                      "note": "opening balance: cost and date unknown"},
                                                ext="opening"))  # fmt: skip
            pending: list[
                tuple[date, str, Decimal]
            ] = []  # stamp duty / STT rows to attach to a same-day trade
            for t in sc.get("transactions") or []:
                ttype = str(t.get("type") or "UNKNOWN")
                day = _cas_day(t.get("date"))
                units, amount, nav = _dec(t.get("units")), _dec(t.get("amount")), _dec(t.get("nav"))
                desc = str(t.get("description") or "")[:200]
                if day is None:
                    res.skipped["no date"] += 1
                    continue
                meta = {**base_meta, "cas_type_txn": ttype, "description": desc}
                if ttype in BUY_TYPES or (ttype == "REVERSAL" and units and units > 0):
                    res.txns.append(ImportedTxn(**common, day=day, kind="buy", quantity=abs(units or 0), price=nav,
                                                amount=abs(amount) if amount is not None else None, meta=meta,
                                                ext=desc))  # fmt: skip
                elif ttype in SELL_TYPES:
                    res.txns.append(ImportedTxn(**common, day=day, kind="sell", quantity=abs(units or 0), price=nav,
                                                amount=abs(amount) if amount is not None else None, meta=meta,
                                                ext=desc))  # fmt: skip
                elif ttype == "DIVIDEND_REINVEST":
                    res.txns.append(ImportedTxn(**common, day=day, kind="buy", quantity=abs(units or 0), price=nav,
                                                amount=abs(amount) if amount is not None else None,
                                                meta={**meta, "reinvest": True}, ext=desc))  # fmt: skip
                    if amount:
                        res.txns.append(ImportedTxn(**common, day=day, kind="dividend", amount=abs(amount),
                                                    meta={**meta, "reinvest": True}, ext=desc))  # fmt: skip
                elif ttype == "DIVIDEND_PAYOUT":
                    if amount:
                        res.txns.append(ImportedTxn(**common, day=day, kind="dividend", amount=abs(amount),
                                                    meta=meta, ext=desc))  # fmt: skip
                elif ttype in ("SWITCH_IN_MERGER", "GIFT_IN", "SEGREGATION"):
                    res.txns.append(ImportedTxn(**common, day=day, kind="opening", quantity=abs(units or 0),
                                                meta={**meta, "note": f"{ttype.lower().replace('_', ' ')}: cost and "
                                                      "holding period carry over from the original units; enter them"},
                                                ext=desc))  # fmt: skip
                    res.warnings.append(f"{name}: {ttype} on {day} imported with an unknown cost (enter it)")
                elif ttype in ("SWITCH_OUT_MERGER", "GIFT_OUT") or (
                    ttype == "REVERSAL" and units and units < 0
                ):
                    res.txns.append(ImportedTxn(**common, day=day, kind="remove", quantity=abs(units or 0),
                                                meta={**meta, "reversal": ttype == "REVERSAL"}, ext=desc))  # fmt: skip
                elif ttype in CHARGE_TYPES and amount:
                    pending.append((day, ttype, abs(amount)))
                else:
                    res.skipped[ttype] += 1
            for day, ttype, amt in pending:
                # stamp duty is levied on the purchase and is part of its cost of acquisition. STT is charged on the
                # redemption and is NOT deductible in computing capital gains (s.48, fifth proviso, inserted by the
                # Finance (No.2) Act 2004): it is recorded on the sale for reference, never netted off the proceeds.
                kinds = ("buy",) if ttype == "STAMP_DUTY_TAX" else ("sell",)
                target = next((x for x in res.txns if x.account == account and x.name == name and x.day == day
                               and x.kind in kinds), None)  # fmt: skip
                if target is None:
                    res.skipped[ttype] += 1
                    continue
                if ttype == "STT_TAX":
                    target.meta["stt"] = str(Decimal(target.meta.get("stt") or 0) + amt)
                    continue
                target.charges += amt
                target.meta.setdefault("charges_from", []).append(ttype)
            val = sc.get("valuation") or {}
            ik = instrument_key("mf", common["isin"], None, None, common["scheme_code"], name)
            res.closing.append(ClosingBalance(ik, account, name, _dec(sc.get("close")) or Decimal(0),
                                              _dec(val.get("nav")), _dec(val.get("value")), _cas_day(val.get("date")),
                                              _dec(val.get("cost"))))  # fmt: skip
            calc, close = _dec(sc.get("close_calculated")), _dec(sc.get("close"))
            if calc is not None and close is not None and abs(calc - close) > Decimal("0.001"):
                res.warnings.append(f"{name}: the statement's closing units {close} differ from its transactions "
                                    f"({calc}); a row may be missing")  # fmt: skip
    return res


def _demat_holdings(data: Any, file_type: str) -> ImportResult:
    d = data.model_dump(mode="json", by_alias=True)
    res = ImportResult("cas", file_type, [], holdings_only=True)
    period = d.get("statement_period") or {}
    res.period = (str(period.get("from") or ""), str(period.get("to") or ""))
    for acc in d.get("accounts") or []:
        account = f"{acc.get('name') or file_type} demat"[:80]
        for e in acc.get("equities") or []:
            ik = instrument_key("stock", e.get("isin"), e.get("symbol"), None, None, e.get("name") or "")
            res.closing.append(ClosingBalance(ik, account, e.get("name") or e.get("isin") or "",
                                              _dec(e.get("num_shares")) or Decimal(0), _dec(e.get("price")),
                                              _dec(e.get("value"))))  # fmt: skip
        for m in acc.get("mutual_funds") or []:
            ik = instrument_key("mf", m.get("isin"), None, None, m.get("amfi"), m.get("name") or "")
            res.closing.append(ClosingBalance(ik, account, m.get("name") or m.get("isin") or "",
                                              _dec(m.get("balance")) or Decimal(0), _dec(m.get("nav")),
                                              _dec(m.get("value")), cost=_dec(m.get("total_cost"))))  # fmt: skip
    res.warnings.append("NSDL/CDSL statements list holdings only (no transactions): used to reconcile units, not "
                        "imported as lots. Import your broker tradebooks for the cost basis.")  # fmt: skip
    return res


# --------------------------------------------------------------------------- broker tradebooks
def _norm(h: Any) -> str:
    return re.sub(r"\s+", " ", str(h or "").strip().lower().replace("_", " "))


BROKERS: dict[str, set[str]] = {
    "zerodha": {"symbol", "isin", "trade date", "trade type", "quantity", "price", "trade id"},
    "groww": {"stock name", "symbol", "isin", "type", "quantity", "value", "execution date and time"},
    "upstox": {"date", "company", "scrip code", "side", "quantity", "price", "trade num"},
}


def read_table(content: bytes, filename: str = "") -> list[list[Any]]:
    """Rows of a CSV or XLSX file (the first sheet), as lists of cells."""
    if content[:2] == b"PK" or filename.lower().endswith((".xlsx", ".xlsm")):
        try:
            from openpyxl import load_workbook

            wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        except Exception as e:
            raise StatementError(f"could not open the spreadsheet ({type(e).__name__})") from e
        ws = wb.worksheets[0]
        return [list(r) for r in ws.iter_rows(values_only=True)]
    text = content.decode("utf-8-sig", "replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return [row for row in csv.reader(io.StringIO(text), dialect)]


def detect_broker(rows: list[list[Any]]) -> tuple[str, int] | None:
    """(broker, header row index) for the first row (of the first 30) whose cells contain a broker's signature."""
    for i, row in enumerate(rows[:30]):
        cells = {_norm(c) for c in row if c is not None}
        for broker, sig in BROKERS.items():
            if sig <= cells:
                return broker, i
    return None


def parse_tradebook(content: bytes, filename: str = "", broker: str | None = None) -> ImportResult:
    rows = read_table(content, filename)
    found = detect_broker(rows)
    if found is None:
        raise StatementError("could not recognise the columns: expected a Zerodha Console tradebook, a Groww order "
                           "history or an Upstox trade book (see Help for the expected headers)")  # fmt: skip
    detected, hi = found
    if broker and broker != detected:
        raise StatementError(f"the columns look like a {detected} file, not {broker}")
    header = [_norm(c) for c in rows[hi]]
    col = {h: j for j, h in enumerate(header)}
    res = ImportResult("tradebook", detected, [])
    account = {"zerodha": "Zerodha", "groww": "Groww", "upstox": "Upstox"}[detected]

    def cell(r: list[Any], name: str) -> Any:
        j = col.get(name)
        return r[j] if j is not None and j < len(r) else None

    date_col = {"zerodha": "trade date", "groww": "execution date and time", "upstox": "date"}[detected]
    month_first = infer_month_first([cell(r, date_col) for r in rows[hi + 1 :]])
    if month_first:
        res.warnings.append("dates read as month-day-year (the file has dates such as 01-13-2025)")
    for r in rows[hi + 1 :]:
        if not any(c not in (None, "") for c in r):
            continue
        try:
            t = _trade_row(detected, r, cell, account, month_first)
        except ValueError as e:
            res.skipped[str(e)] += 1
            continue
        if t is not None:
            res.txns.append(t)
    if not res.txns:
        res.warnings.append("no equity trades found in the file")
    days = [t.day for t in res.txns]
    if days:
        res.period = (min(days).isoformat(), max(days).isoformat())
    res.warnings.append("Tradebooks do not include charges, bonus/split shares, IPO allotments or transfers: add them "
                        "manually or sync corporate actions")  # fmt: skip
    return res


def _symbol(v: Any) -> str:
    """A tradebook's symbol cell as text. Debt and BSE-only rows carry the BSE scrip code there, which a spreadsheet
    stores as a number (941149 -> 941149.0): give it back as digits so it is used as a BSE code, not an NSE symbol."""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    t = str(v or "").strip().upper()
    return t[:-2] if re.fullmatch(r"\d{5,7}\.0", t) else t


def _trade_row(
    broker: str, r: list[Any], cell, account: str, month_first: bool = False
) -> ImportedTxn | None:
    if broker == "zerodha":
        seg = _norm(cell(r, "segment"))
        if seg and seg not in ("eq", "equity"):
            raise ValueError(f"segment {seg.upper()} (not equity delivery)")
        side = _norm(cell(r, "trade type"))
        qty, price = _dec(cell(r, "quantity")), _dec(cell(r, "price"))
        day = parse_day(cell(r, "trade date"), month_first)
        sym, isin = _symbol(cell(r, "symbol")), str(cell(r, "isin") or "").strip().upper()
        exch = str(cell(r, "exchange") or "").strip().upper() or None
        ext = f"{exch}:{cell(r, 'trade id')}:{cell(r, 'order id')}"
        name = sym or isin
        meta = {"exchange": exch, "trade_id": str(cell(r, "trade id") or ""), "order_id": str(cell(r, "order id") or ""),
                "series": cell(r, "series")}  # fmt: skip
    elif broker == "groww":
        status = _norm(cell(r, "order status"))
        if status and status != "executed":
            raise ValueError(f"order status {status}")
        side = _norm(cell(r, "type"))
        qty, value = _dec(cell(r, "quantity")), _dec(cell(r, "value"))
        price = (value / qty) if value is not None and qty else None
        day = parse_day(cell(r, "execution date and time"), month_first)
        sym, isin = _symbol(cell(r, "symbol")), str(cell(r, "isin") or "").strip().upper()
        exch = str(cell(r, "exchange") or "").strip().upper() or None
        ext = f"{exch}:{cell(r, 'exchange order id')}"
        name = str(cell(r, "stock name") or sym or isin).strip()
        meta = {"exchange": exch, "order_id": str(cell(r, "exchange order id") or "")}
    else:  # upstox
        itype = _norm(cell(r, "instrument type"))
        if (cell(r, "strike price") not in (None, "", "0", 0, "-") or cell(r, "expiry") not in (None, "", "-")
                or itype in ("fut", "opt", "futidx", "optidx", "futstk", "optstk")):  # fmt: skip
            raise ValueError("derivatives row (not equity delivery)")
        side = _norm(cell(r, "side"))
        qty, price = _dec(cell(r, "quantity")), _dec(cell(r, "price"))
        day = parse_day(cell(r, "date"), month_first)
        sym, isin = "", ""
        exch = str(cell(r, "exchange") or "").strip().upper() or None
        ext = f"{exch}:{cell(r, 'trade num')}"
        name = str(cell(r, "company") or "").strip()
        meta = {"exchange": exch, "trade_num": str(cell(r, "trade num") or ""),
                "scrip_code": str(cell(r, "scrip code") or "")}  # fmt: skip
    if side not in ("buy", "sell", "b", "s"):
        raise ValueError(f"side {side or 'missing'}")
    if day is None:
        raise ValueError("unreadable date")
    if not qty or qty <= 0 or price is None or price < 0:
        raise ValueError("missing quantity or price")
    if not name:
        raise ValueError("no symbol, ISIN or name")
    kind = "buy" if side in ("buy", "b") else "sell"
    return ImportedTxn(account=account, asset_type="stock", name=name, day=day, kind=kind, quantity=qty, price=price,
                       amount=qty * price, source=broker, isin=isin or None,
                       nse_symbol=(sym or None) if not sym.isdigit() else None,
                       bse_code=sym if sym.isdigit() else None,
                       meta={k: v for k, v in meta.items() if v not in (None, "")}, ext=ext)  # fmt: skip


# --------------------------------------------------------------------------- broker holdings statements
# A holdings statement is a snapshot (instrument, quantity, average price) with no dates: it feeds the broker-baseline
# merge rules (connectors.merge), exactly like a holdings API response. Column names are matched loosely (normalised,
# first match wins) because brokers rename them; see docs/dev/BROKER_SETUP.md for how to download each.
#   Zerodha Console → Portfolio → Holdings → download (XLSX): Symbol, ISIN, Sector, Quantity Available, Quantity
#     Discrepant, Quantity Long Term, Quantity Pledged (Margin), Quantity Pledged (Loan), Average Price, Previous
#     Closing Price, Unrealized P&L ... [U: from open-source importers and Zerodha support pages, not a raw file]
#   Groww → Stocks → Holdings → download statement (XLSX): Stock Name, ISIN, Quantity, Average buy price, Buy value,
#     Closing price, Closing value, Unrealised P&L [U]
#   Any other broker: a table with an ISIN column, a quantity column and an average-price column (pass the broker).
_HS_NAME = ("symbol", "stock name", "instrument", "company name", "scrip name", "security name", "name")
_HS_QTY = ("quantity available", "quantity", "qty", "qty.", "net qty", "total quantity", "free quantity")
_HS_AVG = ("average price", "average buy price", "avg. cost", "avg cost", "avg. price", "avg price", "buy avg",
           "buy average", "average cost")  # fmt: skip
_HS_EXTRA_QTY = ("quantity discrepant", "quantity pledged (margin)", "quantity pledged (loan)")
_HS_CLOSE = ("previous closing price", "closing price", "ltp", "last traded price", "close price")


def _first(col: dict[str, int], names: tuple[str, ...]) -> int | None:
    return next((col[n] for n in names if n in col), None)


def detect_holdings_statement(rows: list[list[Any]]) -> tuple[str | None, int] | None:
    """(broker or None, header row) for the first row that has ISIN, quantity and average-price columns and no
    trade-date column (a tradebook has one)."""
    for i, row in enumerate(rows[:40]):
        cells = {_norm(c) for c in row if c is not None}
        if "isin" not in cells or "trade date" in cells or "execution date and time" in cells:
            continue
        if not any(q in cells for q in _HS_QTY) or not any(a in cells for a in _HS_AVG):
            continue
        broker = ("zerodha" if "quantity available" in cells or "quantity long term" in cells
                  else "groww" if "average buy price" in cells or "stock name" in cells else None)  # fmt: skip
        return broker, i
    return None


def realised_report_hint(content: bytes, filename: str = "") -> str | None:
    """A broker's realised P&L / capital-gains report (buy date + sell date per closed lot) is not a statement the app
    can import (it has no open positions and repeats trades the order history has): say so and what to use instead."""
    try:
        rows = read_table(content, filename)
    except Exception:  # unreadable files keep the generic message
        return None
    for row in rows[:40]:
        cells = {_norm(c) for c in row if c is not None}
        if "buy date" in cells and "sell date" in cells:
            return ("this is a P&L / capital-gains report (realised trades only), which the app does not import: use "
                    "the Order history (Groww: Stocks → Reports → Order history; Zerodha: Console → Tradebook) and the "
                    "Holdings statement instead. The app computes P&L and capital gains itself from those.")  # fmt: skip
    return None


def parse_holdings_statement(content: bytes, filename: str = "", broker: str | None = None):
    """A broker holdings statement → (broker, [BrokerHolding]). Raises StatementError when it is not one."""
    from finresearch.portfolio.connectors.base import BrokerHolding

    rows = read_table(content, filename)
    found = detect_holdings_statement(rows)
    if found is None:
        raise StatementError("could not recognise the columns: expected a tradebook (Zerodha/Groww/Upstox) or a "
                             "holdings statement with ISIN, quantity and average-price columns")  # fmt: skip
    detected, hi = found
    broker = broker or detected
    if broker not in ("zerodha", "groww", "upstox"):
        raise StatementError("this looks like a holdings statement, but the broker is not clear: choose it")
    col = {}
    for j, h in enumerate(rows[hi]):
        col.setdefault(_norm(h), j)
    ji, jn, jq, ja = col["isin"], _first(col, _HS_NAME), _first(col, _HS_QTY), _first(col, _HS_AVG)
    jc = _first(col, _HS_CLOSE)
    extra = [col[n] for n in _HS_EXTRA_QTY if n in col]
    out: list[BrokerHolding] = []

    def at(r: list[Any], j: int | None) -> Any:
        return r[j] if j is not None and j < len(r) else None

    for r in rows[hi + 1 :]:
        isin = str(at(r, ji) or "").strip().upper()
        if not ISIN_RE.match(isin):
            continue  # totals, blank and note rows
        q = _dec(at(r, jq)) or Decimal(0)
        q += sum((_dec(at(r, j)) or Decimal(0) for j in extra), Decimal(0))
        if q <= 0:
            continue
        name = str(at(r, jn) or isin).strip()
        sym = (
            name.upper() if broker == "zerodha" and re.fullmatch(r"[A-Z0-9&\-]{1,20}", name.upper()) else None
        )
        out.append(BrokerHolding(name=name, quantity=q, isin=isin, symbol=sym, avg_price=_dec(at(r, ja)),
                                 last_price=_dec(at(r, jc))))  # fmt: skip
    if not out:
        raise StatementError("no holdings with an ISIN and a quantity found in the statement")
    return broker, out
