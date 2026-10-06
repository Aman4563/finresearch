"""Synthetic monthly-portfolio workbooks in the quant and Tata layouts (#214).

The layouts copy the fund houses' Aug-2026 files as read on 06-Oct-2026 (row order, header wording, section labels,
total rows, the notes below the table); the companies, ISINs and numbers are made up. Built in code so the repository
holds no binary copy of a fund house's file.
"""

from __future__ import annotations

import io
from datetime import datetime


def _book(sheets: list[tuple[str, list[list]]]) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    for title, rows in sheets:
        ws = wb.create_sheet(title)
        for r in rows:
            ws.append(r)
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


def quant_rows(name: str = "quant Example Flexi Cap Fund", futures_pct: float | None = 25.3) -> list[list]:
    """One scheme per file: house, scheme, description, date line, blank rows, then SR | ISIN | NAME | RATING |
    INDUSTRY | QUANTITY | MARKET VALUE(Rs.in Lakhs) | % to NAV | YTM; weights in percent, Grand Total 100."""
    n = None
    return [
        [n, n, "quant Mutual Fund"], [n, n, name],
        [n, n, "An open-ended dynamic equity investing across large cap, mid cap, small cap stocks"],
        [n, n, "MONTHLY PORTFOLIO STATEMENT AS ON 31 Aug 2026"], [n, n, ""], [n, n, ""],
        ["SR", "ISIN", "NAME OF THE INSTRUMENT", "RATING", "INDUSTRY", "QUANTITY", "MARKET VALUE(Rs.in Lakhs)",
         "% to NAV", "YTM"],
        [n, "", "EQUITY & EQUITY RELATED"], [n, "", "(a) Listed / awaiting listing on Stock Exchanges"],
        [1, "INE00QA01011", "Alpha Motors Ltd", "N.A.", "Auto Components", 1000, 400.0, 40.0, "-"],
        [2, "INE00QB01019", "Beta Power Ltd", "N.A.", "Power", 500, 300.0, 30.0, "-"],
        [n, "", "Sub Total", "", "", "", 700.0, 70.0],
        [n, "", "(b) Index / Stock Options"], [n, "", "Sub Total"], [n, "", "Total", "", "", "", 700.0, 70.0],
        [n, "", "MONEY MARKET INSTRUMENTS"], [n, "", "c) Treasury Bills"],
        [3, "IN002026X099", "91 Days Treasury Bill 03-Sep-2026", "SOV", "N.A.", 100000, 100.0, 10.0, 5.2],
        [n, "", "Sub Total", "", "", "", 100.0, 10.0], [n, "", "OTHERS"], [n, "", "(c) Tri Party Repo (TREPs)"],
        [4, "INCBLO010926", "TREPS 01-Sep-2026 DEPO 10", "N.A.", "N.A.", 1000, 250.0, 25.0, 5.7],
        [n, "", "(d) Other Receivables (Payables)"],
        [5, "", "NCA-NET CURRENT ASSETS", "N.A.", "N.A.", "", -50.0, -5.0],
        [n, "", "Grand Total", "", "", "", 1000.0, 100],
        [n, "Disclosure for investment in derivative instruments: "],
        [n, "Other than Hedging Positions through Futures as on 31/08/2026"],
        [n, "Underlying", "Long / Short", "Future Price when Purchased", "Current price of the contract",
         "Margin maintained in Rs.Lakhs"],
        [n, "Gamma Pharma Limited 29/09/2026", "Long", 100.5, 101.0, "Refer Note 11"],
        [n, "Total Exposure due to futures (non hedging positions) as a %age of net assets", n,
         futures_pct if futures_pct is not None else "NIL"],
        [n, "Risk-o-meter of the Benchmark- NIFTY 500 TRI"],
    ]  # fmt: skip


def quant_xlsx(name: str = "quant Example Flexi Cap Fund", futures_pct: float | None = 25.3) -> bytes:
    return _book([(name.replace(" ", "_")[:31], quant_rows(name, futures_pct))])


def _tata_scheme(upper: str, proper: str) -> list[list]:
    """One Tata scheme sheet: an 'Index' link cell, the name twice, the description and suitability blurb, the date
    as 'Portfolio as on 31-08-26', then the equity header, and a second header (RATINGS) above the debt block."""
    n = None
    head = [n, "NAME OF THE INSTRUMENT", "YIELD ( IN % )", "INDUSTRY", "ISIN CODE", "QUANTITY", "MKT VAL(Rs. Lacs)",
            "% to NAV", ""]
    return [
        ["Index", upper], [n], [n, proper], [n, "(An open ended equity scheme investing across market caps)"],
        [n, "This product is suitable for investors who are seeking*:"], [n, "•Long Term Capital Appreciation"],
        [n, "•Investment in equity/equity related instruments of companies across market caps."],
        [n, "*Investors should consult their financial advisors if in doubt about whether the product is suitable for "
            "them", "Scheme Risk-O-Meter", "Benchmark Risk-O-Meter"],
        [n], [n, "Portfolio as on 31-08-26"], [n], head,
        [n, "EQUITY & EQUITY RELATED"], [n, "A) LISTED/AWAITING LISTING ON STOCK EXCHANGES"],
        [n, "Delta Bank Ltd", n, "Banks", "INE00TA01014", 200, 550.0, 55.0],
        [n, "Epsilon Tech Ltd", n, "IT - Software", "INE00TB01012", 100, 300.0, 30.0],
        [n, "Example Nifty ETF", n, "CAPITAL MARKETS", "INF00TC01AB3", 50, 20.0, 2.0],
        [n, "B) UNLISTED"], [n, "Nil", "Nil", "Nil", "Nil", "Nil", "Nil", "Nil"],
        [n, "EQUITY & EQUITY RELATED TOTAL", n, n, n, n, 870.0, 87.0],
        [n, "NAME OF THE INSTRUMENT", "YIELD ( IN % )", "RATINGS", "ISIN CODE", "QUANTITY", "MKT VAL(Rs. Lacs)",
         "% to NAV"],
        [n, "DEBT INSTRUMENTS"], [n, "(I) GOVERNMENT SECURITIES"],
        [n, "7.10% GOI 2034", 6.4, "SOVEREIGN", "IN0020240019", 50000, 50.0, 5.0],
        [n, "GOVERNMENT SECURITIES TOTAL", n, n, n, n, 50.0, 5.0],
        [n, "H) FOREIGN SECURITIES AND /OR OVERSEAS ETF(S)"], [n, "Nil", "Nil", "Nil", "Nil", "Nil", "Nil", "Nil"],
        [n, "FOREIGN SECURITIES AND /OR OVERSEAS ETF(S) TOTAL", n, n, n, n, 0, 0],
        [n, "I) REPO", n, n, n, n, 40.0, 4.0], [n, "PORTFOLIO TOTAL", n, n, n, n, 960.0, 96.0],
        [n, "CASH / NET CURRENT ASSET", n, n, n, n, 40.0, 4.0], [n, "NET ASSETS", n, n, n, n, 1000.0, 100],
        [n, "* % OF MARKET VALUE OF SECURITIES TO NET ASSETS IS < 0.01"],
        [n, "NAV AS ON 31-AUG-26: RS.44.7303 (DIRECT - GROWTH)"],
        [n, "^ Hedging positions through futures as on ", datetime(2026, 8, 31)],
        [n, "Underlying", "Long/Short", "Future price when purchased", "Current Price of the contract",
         "Margin maintained in Rs.lakhs"],
        [n, "NIL", "NIL", "NIL", "NIL", "NIL"],
    ]  # fmt: skip


def tata_xlsx() -> bytes:
    """The house-wide workbook: an Index sheet (classification, code, name), a risk-o-meter sheet without holdings,
    then one sheet per scheme named by its code."""
    return _book([
        ("Index", [["Index"], ["CLASSIFICATION", "SCHEME CODE", "SCHEME NAME"],
                   ["Equity", "TEXFLX", "TATA EXAMPLE FLEXI CAP FUND"], ["Equity", "TEXSML", "TATA EXAMPLE SMALL CAP FUND"]]),
        ("Tata Scheme Risk-o-Meter", [["Scheme Risk-O-Meter and Scheme Benchmark Risk-O-Meter as of 31st August 2026"],
                                      ["Serial No.", "Scheme Name", "Scheme Risk-O-Meter", "Scheme Bench Mark"],
                                      [1, "TATA EXAMPLE FLEXI CAP FUND", "Very High", "Nifty 500"]]),
        ("TEXFLX", _tata_scheme("TATA EXAMPLE FLEXI CAP FUND", "Tata Example Flexi Cap Fund")),
        ("TEXSML", _tata_scheme("TATA EXAMPLE SMALL CAP FUND", "Tata Example Small Cap Fund")),
    ])  # fmt: skip
