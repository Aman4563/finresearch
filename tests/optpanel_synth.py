"""Synthetic UDiFF F&O bhavcopies for the options-panel tests (#374): made-up underlyings priced with Black-76 at one
flat volatility, so every IV the panel fits is known in advance. No real prices, no network."""

from __future__ import annotations

import io
import math
import zipfile
from datetime import date

from finresearch.fincalc.fno_history import RATE, black76_price

HEADER = ("TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,StrkPric,"
          "OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,"
          "ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4")  # fmt: skip


def line(day: date, kind: str, sym: str, exp: date, strike: float | None, right: str, settle: float, *,
         oi: int, contracts: int, spot: float, lot: int, close: float | None = None) -> str:  # fmt: skip
    close = settle if close is None else close
    k = "" if strike is None else f"{strike:.2f}"
    cells = [day.isoformat(), day.isoformat(), "FO", "NSE", kind, "1", "", sym, "", exp.isoformat(), exp.isoformat(),
             k, right, f"{sym}X", f"{close:.2f}", f"{close:.2f}", f"{close:.2f}", f"{close:.2f}", f"{close:.2f}",
             f"{close:.2f}", f"{spot:.2f}", f"{settle:.2f}", str(oi), "0", str(contracts),
             f"{contracts * lot * settle:.2f}", str(contracts), "F1", str(lot), "", "", "", "", ""]  # fmt: skip
    return ",".join(cells)


def underlying(day: date, sym: str, spot: float, vol: float, expiries: list[date], *, kind: str = "STO",
               lot: int = 100, step_pct: float = 0.02, width: float = 0.30, futures: bool = True,
               atm_contracts: int = 200) -> list[str]:  # fmt: skip
    """Futures (when `futures`) and options at strikes spot x (1 +/- k x step_pct) out to `width`. Contracts fall
    off with distance from ATM; strikes beyond 20 % trade nothing but carry OI."""
    out = []
    fut_kind = "STF" if kind == "STO" else "IDF"
    for e in expiries:
        t = (e - day).days / 365
        f = spot * math.exp(RATE * t)
        if futures:
            out.append(
                line(
                    day,
                    fut_kind,
                    sym,
                    e,
                    None,
                    "",
                    round(f, 2),
                    oi=lot * 50,
                    contracts=500,
                    spot=spot,
                    lot=lot,
                )
            )
        n = round(width / step_pct)
        for i in range(-n, n + 1):
            k = round(spot * (1 + i * step_pct), 2)
            dist = abs(math.log(k / f))
            contracts = 0 if dist > 0.2 else max(atm_contracts - 40 * abs(i), min(12, atm_contracts))
            for right, name in (("CE", "call"), ("PE", "put")):
                px = (
                    black76_price(name, f, k, t, RATE, vol)
                    if t > 0
                    else max(0.0, (f - k) if name == "call" else (k - f))
                )
                px = max(round(px, 2), 0.05)
                out.append(
                    line(
                        day, kind, sym, e, k, right, px, oi=lot * 10, contracts=contracts, spot=spot, lot=lot
                    )
                )
    return out


def csv_text(lines: list[str]) -> str:
    return "\n".join([HEADER, *lines]) + "\n"


def zipped(text: str, name: str = "bhav.csv") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, text)
    return buf.getvalue()
