"""A SYNTHETIC FBIL G-sec workbook (.xlsx), built in the tests (#479): the layout of FBIL's file (sheets "G-Sec" and
"Par Yield", a title row with the date, the 'Tenor (Year)' header) with made-up numbers and fake ISINs. No FBIL data
is kept in this public repository: FBIL's Terms of Use of 13-Oct-2026 forbid redistributing it (#476).

The par yields are made up: 5.00 % half-yearly at 0.25 years, rising 0.20 percentage points per listed tenor, and the
annualised column is the half-yearly rate made effective annual, (1 + y/2)^2 - 1."""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

TENORS = ("0.25", "0.5", "1", "2", "3", "3.25", "5", "10", "15", "30", "40", "50")
SEMI = tuple(Decimal("5.00") + Decimal("0.20") * i for i in range(len(TENORS)))  # % half-yearly
AS_OF = date(2026, 1, 7)


def annualised(semi_pct: Decimal) -> Decimal:
    y = semi_pct / 100
    return (((1 + y / 2) ** 2 - 1) * 100).quantize(Decimal("0.01"))


def gsec_xlsx(as_of: date = AS_OF) -> bytes:
    """The workbook's bytes, as FBIL's site would serve them (an .xlsx body under an .xls name)."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "G-Sec"
    ws.append([None, "Synthetic Benchmarks Ltd (test data)"])
    ws.append(["FBIL GSec Prices/Yields", None, f"{as_of:%d-%b-%Y}"])
    ws.append(["ISIN", "Coupon", "Maturity(dd-mmm-yyyy)", "Price(Rs)", "YTM% p.a. (Semi-Annual)", "Remark 1"])
    ws.append(["IN0000000017", 7.0, "01-Jan-2030", 100.0, 7.0])  # fake ISINs, made-up bonds
    ws.append(["IN0000000025", 6.5, "01-Jan-2035", 99.0, 6.64])
    par = wb.create_sheet("Par Yield")
    par.append(["Synthetic Benchmarks Ltd (test data)"])
    par.append(["FBIL GSec Base/Par Yield", None, f"{as_of:%d-%b-%Y}"])
    par.append(["Tenor (Year)", "YTM% p.a.(Semi-Annual)", "YTM % p.a.(Annualized)"])
    for t, s in zip(TENORS, SEMI, strict=True):
        par.append([float(t), float(s), float(annualised(s))])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
