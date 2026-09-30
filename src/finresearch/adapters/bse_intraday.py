"""BSE intraday price series: today's (or the last session's) 1-minute series for a BSE scrip, with BSE volume.

Endpoint (api.bseindia.com, the BSE quote page's own chart; verified live 30-Sep-2026 with `bse.BSE_HEADERS`, no
cookies): `/BseIndiaAPI/api/StockReachGraph/w?scripcode=<code>&flag=1D&fromdate=&todate=&seriesid=`
      -> {"CurrDate", "PrevClose", "CurrVal", "CurrTime", "Data": "<JSON string>"}
      where Data = [{"dttm": "Wed Sep 30 2026 09:15:59", "vale1": "996.00", "vole": "24885"}, ...] (newest first)

- One last-traded-price sample a minute, stamped hh:mm:59 in IST, from 09:15:59; `vole` is the shares traded on BSE in
  that minute (BSE only: NSE's volume is not included). The newest point is the current minute (near real time).
- `flag=5D` answers five sessions at 10-minute resolution and `flag=1M` daily closes; the app uses 1D only and
  archives each session itself (monitor.intraday), exactly as for NSE, so 5D candles keep one resolution.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from finresearch.adapters.bse import BseClient, BseError
from finresearch.adapters.http import IST
from finresearch.adapters.nse import parse_num
from finresearch.adapters.nse_intraday import IntradaySeries
from finresearch.fincalc.candles import Tick, normalise

GRAPH_PATH = "/StockReachGraph/w"


def graph_url(code: str) -> str:
    return f"https://api.bseindia.com/BseIndiaAPI/api{GRAPH_PATH}?scripcode={code}&flag=1D"


def parse_graph(data: dict[str, Any], key: str, code: str) -> IntradaySeries:
    raw = data.get("Data")
    try:
        rows = json.loads(raw) if isinstance(raw, str) else (raw or [])
    except ValueError as e:
        raise BseError(f"BSE intraday series for {code} is not JSON") from e
    ticks = []
    for r in rows if isinstance(rows, list) else []:
        try:
            at = datetime.strptime(str(r.get("dttm", "")).strip(), "%a %b %d %Y %H:%M:%S").replace(tzinfo=IST)
        except ValueError:
            continue
        price = parse_num(r.get("vale1"))
        if price is None:
            continue
        ticks.append(Tick(at, price, "NM", parse_num(r.get("vole"))))
    prev = parse_num(data.get("PrevClose"))
    return IntradaySeries(key, "equity", prev if prev else None, normalise(ticks), graph_url(code))


async def bse_intraday(client: BseClient, code: str) -> IntradaySeries:
    data, _ = await client.get_json(GRAPH_PATH, {"scripcode": code, "flag": "1D", "fromdate": "", "todate": "",
                                                 "seriesid": ""})  # fmt: skip
    if not isinstance(data, dict):
        raise BseError(f"BSE intraday series for {code}: unexpected payload")
    return parse_graph(data, f"BSE:{code}", code)
