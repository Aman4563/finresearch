"""A synthetic, password-protected CAMS "detailed" CAS PDF, generated at test time for the real casparser to parse.

Everything in it is invented (investor, folio, schemes, amounts, the ISIN-like codes); no personal data is stored in
the repository, and no PDF is committed (``*.pdf`` is gitignored). The layout mimics what casparser's CAMS parser
reads: the CAMSCASWS marker, the "Consolidated Account Statement" heading with the period, the investor block
(Email Id ... Mobile) at the top left, the six-column transaction header, then per scheme a folio line, a
``<code>-<name> ... Registrar : CAMS`` line, "Opening Unit Balance", dated rows and the closing footer.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field

# Helvetica advance widths (per 1000 em) for the characters used in right-aligned numbers and headers
_W = {**{c: 556 for c in "0123456789"}, ",": 278, ".": 278, "(": 333, ")": 333, "-": 333, " ": 278, "A": 667,
      "m": 833, "o": 556, "u": 556, "n": 556, "t": 278, "U": 722, "i": 222, "s": 500, "P": 667, "r": 333, "c": 500,
      "e": 556, "I": 278, "N": 722, "R": 722, "B": 667, "a": 556, "l": 222}  # fmt: skip
SIZE = 7


def _width(text: str, size: float = SIZE) -> float:
    return sum(_W.get(c, 556) for c in text) * size / 1000


def _pdf(pages: list[list[tuple[float, float, str, float]]]) -> bytes:
    objs: list[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>"]
    n = len(pages)
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    font = 3 + 2 * n
    for i, items in enumerate(pages):
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {4 + 2 * i} 0 R "
                    f"/Resources << /Font << /F1 {font} 0 R >> >> >>".encode())  # fmt: skip
        ops = []
        for x, y, t, sz in items:
            t = t.replace("\\", "\\\\").replace("(", r"\(").replace(")", r"\)")
            ops.append(f"BT /F1 {sz} Tf 1 0 0 1 {x:.2f} {y:.2f} Tm ({t}) Tj ET")
        stream = "\n".join(ops).encode()
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for k, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{k} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


@dataclass
class Scheme:
    code: str  # RTA scheme code, e.g. "EXFG"
    name: str
    isin: str
    opening: str  # units, e.g. "0.000"
    rows: list[tuple[str, str, str, str, str, str]]  # date, description, amount, units, price, balance
    close: str
    nav_day: str
    nav: str
    value: str
    cost: str
    amc: str = "Example Mutual Fund"
    folio: str = "1234567890 / 12"


@dataclass
class Statement:
    start: str = "01-Apr-2025"
    end: str = "29-Sep-2026"
    schemes: list[Scheme] = field(default_factory=list)


def build_cas(st: Statement, password: str | None = "ABCDE1234F") -> bytes:
    items: list[tuple[float, float, str, float]] = []
    y = 770.0

    def left(x: float, text: str, size: float = SIZE) -> None:
        items.append((x, y, text, size))

    def right(x_end: float, text: str) -> None:
        items.append((x_end - _width(text), y, text, SIZE))

    for line in ("Email Id: synthetic.investor@example.com", "Synthetic Investor", "1 Example Street, Testville",
                 "Mobile: +910000000000"):  # fmt: skip
        left(40, line)
        y -= 10
    y -= 4
    left(250, "Consolidated Account Statement", 10)
    y -= 12
    left(250, f"{st.start} To {st.end}")
    y -= 12
    left(500, "CAMSCASWS")
    y -= 16
    left(40, "Date")
    left(100, "Transaction")
    right(360, "Amount")
    right(420, "Units")
    right(480, "Price")
    right(550, "Unit")
    y -= 8
    right(360, "(INR)")
    right(480, "(INR)")
    right(550, "Balance")
    y -= 16
    current_amc = None
    for sc in st.schemes:
        if sc.amc != current_amc:
            left(40, sc.amc)
            y -= 12
            current_amc = sc.amc
        left(40, f"Folio No: {sc.folio} PAN: ABCDE1234F KYC: OK PAN: OK")
        y -= 10
        left(40, "Synthetic Investor")
        y -= 10
        left(40, f"{sc.code}-{sc.name} - ISIN: {sc.isin}(Advisor: DIRECT) Registrar : CAMS")
        y -= 10
        left(40, f"Opening Unit Balance: {sc.opening}")
        y -= 10
        for d, desc, amt, units, price, bal in sc.rows:
            left(40, d)
            left(100, desc)
            if amt:
                right(360, amt)
            if units:
                right(420, units)
            if price:
                right(480, price)
            if bal:
                right(550, bal)
            y -= 10
        left(40, f"Closing Unit Balance: {sc.close} NAV on {sc.nav_day}: INR {sc.nav} Total Cost Value: {sc.cost} "
                 f"Market Value on {sc.nav_day}: INR {sc.value}")  # fmt: skip
        y -= 16
    pdf = _pdf([items])
    if password:
        from pypdf import PdfReader, PdfWriter

        w = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf)))
        w.encrypt(password, algorithm="RC4-128")
        buf = io.BytesIO()
        w.write(buf)
        pdf = buf.getvalue()
    return pdf


def sample_statement(start: str = "01-Apr-2025") -> Statement:
    """Two invented schemes. Flexi cap: 50 opening units (cost unknown), a purchase, a SIP, a dividend reinvestment
    (with stamp duty) and a redemption; closing 150.000. Short-duration debt: two purchases, closing 300.000."""
    flexi = Scheme(
        "EXFG", "Example Flexi Cap Fund - Direct Plan - Growth", "INF000X01019", "50.000",
        [("10-May-2025", "Purchase", "10,000.00", "100.000", "100.0000", "150.000"),
         ("10-May-2025", "*** Stamp Duty ***", "0.50", "", "", ""),
         ("10-Jun-2025", "SIP Purchase Instalment 1/12", "5,500.00", "50.000", "110.0000", "200.000"),
         ("15-Jul-2025", "IDCW Reinvestment @ Rs.1.00 per unit", "1,200.00", "10.000", "120.0000", "210.000"),
         ("20-Jan-2026", "Redemption", "(7,500.00)", "(60.000)", "125.0000", "150.000")],
        "150.000", "29-Sep-2026", "130.0000", "19,500.00", "16,700.00")  # fmt: skip
    debt = Scheme(
        "EXSD", "Example Short Duration Fund - Direct Plan - Growth", "INF000X01027", "0.000",
        [("05-Aug-2025", "Purchase", "20,000.00", "200.000", "100.0000", "200.000"),
         ("05-Sep-2025", "Purchase", "10,100.00", "100.000", "101.0000", "300.000")],
        "300.000", "29-Sep-2026", "105.0000", "31,500.00", "30,100.00")  # fmt: skip
    return Statement(start=start, schemes=[flexi, debt])
