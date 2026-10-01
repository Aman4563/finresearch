"""Daily validation archive (monitor.archive, #147): recorded NSE fixtures, a fake option-chain client, frozen time."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from finresearch.adapters.fbil import ParCurve, ParPoint
from finresearch.adapters.nse import Quote
from finresearch.fincalc import dates
from finresearch.monitor import archive as ar

PRICES = Path(__file__).parent / "fixtures" / "prices"
NSE = Path(__file__).parent / "fixtures" / "nse"
AFTER_CLOSE = datetime(2026, 9, 30, 16, 30, tzinfo=dates.IST)  # a Wednesday


def _quote(sym: str) -> Quote:
    return Quote.parse(json.loads((PRICES / f"nse_quote_{sym}_20260930_after_close.json").read_text()))


def test_quote_carries_sector_and_symbol_pe():
    q = _quote("INFY")
    assert (q.sector_index, q.sector_pe, q.symbol_pe) == ("NIFTY IT", Decimal("13.2"), Decimal("13.59"))
    assert _quote("TMCV").sector_index is None and _quote("TMCV").sector_pe == Decimal("28.59")  # "-" → None
    row = ar.sector_pe_row(q, date(2026, 9, 30))
    assert row["close"] == Decimal("994.1") and row["symbol"] == "INFY"


def _curve() -> ParCurve:
    pts = [
        ParPoint(Decimal(t), Decimal(y), None) for t, y in (("1", "0.058"), ("5", "0.064"), ("10", "0.067"))
    ]
    return ParCurve(as_of=date(2026, 9, 29), points=pts)


def test_bond_spread_is_ytm_over_the_matching_par_yield_at_an_assumed_yearly_coupon():
    from finresearch.adapters.nse_bonds import ListedBond
    from finresearch.fincalc import bonds as B

    b = ListedBond(symbol="X", series="N1", isin="INE000X01011", coupon_pct=Decimal("9"), face_value=Decimal(1000),
                   last_price=Decimal(1000), close=None, maturity=date(2031, 9, 30), next_interest_date=None,
                   rating="AA", rating_agency=None, traded_value=Decimal(500000))  # fmt: skip
    idle = b.model_copy(update={"isin": "INE000X01029", "traded_value": None})
    rows = ar.bond_spread_rows([b, idle], _curve(), date(2026, 9, 30), date(2026, 9, 30))
    assert len(rows) == 1  # untraded bonds are not archived
    r = rows[0]
    ytm = B.ytm(Decimal(1000), date(2026, 9, 30), date(2031, 9, 30), Decimal("0.09"), 1, Decimal(1000))
    assert float(ytm) == pytest.approx(0.09, abs=1e-6)  # at par, a yearly 9 % coupon yields 9 %
    gsec = _curve().par_yield((date(2031, 9, 30) - date(2026, 9, 30)).days / 365.25)
    want = (B.effective_annual(ytm, 1) - B.effective_annual(gsec, 2)) * 100
    assert r["spread_pp_assumed_yearly"] == pytest.approx(want) and 2.3 < float(want) < 2.6
    assert "yearly" in r["assumption"]


def test_next_expiry_and_term_slope():
    assert ar.next_expiry(
        [date(2026, 10, 13), date(2026, 10, 6), date(2026, 10, 20)], date(2026, 10, 6)
    ) == date(2026, 10, 13)
    assert ar.next_expiry([date(2026, 10, 6)], date(2026, 10, 6)) is None
    near = {"expiry": date(2026, 10, 6), "atm_iv": Decimal("11.5"), "skew_25d": Decimal("2")}
    nxt = {
        "expiry": date(2026, 10, 13),
        "atm_iv": Decimal("12.25"),
        "skew_25d": Decimal("2.5"),
        "day": date(2026, 9, 30),
    }
    assert ar.iv_term_row("NIFTY", near, nxt, date(2026, 9, 30))["term_slope"] == pytest.approx(0.75)


async def test_archive_step_writes_each_stream_once_a_day(tmp_path):
    from test_fno_signal import FNO
    from test_fno_signal import FakeFno as BaseFno

    from finresearch.adapters.nse_fno import parse_chain

    class FakeFno(BaseFno):
        async def option_chain(self, symbol, expiry):  # the recorded chain, served as the expiry asked for
            self._count("option_chain")
            return parse_chain(
                symbol, expiry, json.loads((FNO / "option_chain_NIFTY_20261006_trimmed.json").read_text())
            )

    ar.reset_tries()
    BaseFno.calls = {}
    asked: list[str] = []

    async def quote(sym):
        asked.append(sym)
        if sym == "INFY":
            return _quote("INFY")
        raise RuntimeError("not in the fixtures")

    async def curve():
        return _curve()

    async def bonds():
        from finresearch.adapters.nse_bonds import parse_live_bonds

        return parse_live_bonds(json.loads((NSE / "bonds_live_trimmed.json").read_text()))

    near = {
        "expiry": date(2026, 10, 6),
        "atm_iv": Decimal("11"),
        "skew_25d": Decimal("1"),
        "day": date(2026, 9, 30),
    }
    src = ar.ArchiveSources(quote=quote, fno=FakeFno, bonds=bonds, par_curve=curve, watched=lambda: ["INFY", "ABC"],
                            near_iv=lambda s, d: near if s == "NIFTY" else None, out_dir=tmp_path)  # fmt: skip
    early = await ar.archive_step(datetime(2026, 9, 30, 15, 0, tzinfo=dates.IST), src)
    assert early["skipped"] == "outside the after-close window" and not list(tmp_path.iterdir())
    weekend = await ar.archive_step(datetime(2026, 10, 3, 17, 0, tzinfo=dates.IST), src)
    assert "skipped" in weekend
    # Gandhi Jayanti (Friday 2-Oct-2026): NSE serves 1-Oct's figures, which must not be archived as 2-Oct's
    holiday = await ar.archive_step(datetime(2026, 10, 2, 17, 0, tzinfo=dates.IST), src,
                                    holidays={date(2026, 10, 2)})  # fmt: skip
    assert holiday["skipped"] == "exchange holiday" and not list(tmp_path.iterdir())
    res = await ar.archive_step(AFTER_CLOSE, src)
    assert (
        res["archived"]["sector_pe"] == 1
        and res["archived"]["iv_term"] == 1
        and res["archived"]["gsec_spread"] >= 1
    )
    assert asked.count("INFY") == 1 and "ABC" in asked  # representatives and watches, de-duplicated
    iv = [json.loads(x) for x in (tmp_path / "iv_term.jsonl").read_text().splitlines()]
    assert (
        iv[0]["symbol"] == "NIFTY" and iv[0]["next_expiry"] == "2026-10-13" and iv[0]["near_atm_iv"] == 11.0
    )
    g = [json.loads(x) for x in (tmp_path / "gsec_spread.jsonl").read_text().splitlines()]
    assert g[0]["kind"] == "par_curve" and g[0]["points"][0] == [1.0, 0.058]
    calls = dict(BaseFno.calls)
    assert (
        calls["option_chain"] == 1 and calls["contract_info"] == 1
    )  # NIFTY only: the others had no near row
    again = await ar.archive_step(AFTER_CLOSE.replace(hour=17), src)
    assert again["archived"] == {} and BaseFno.calls == calls  # nothing twice, no extra network
    ar.reset_tries()  # a restarted monitor reads the day back from the files
    assert (await ar.archive_step(AFTER_CLOSE.replace(hour=18), src))["archived"] == {}


async def test_archive_failures_are_retried_at_most_three_times(tmp_path):
    ar.reset_tries()

    async def boom():
        raise RuntimeError("FBIL down")

    src = ar.ArchiveSources(quote=lambda s: boom(), fno=None, bonds=boom, par_curve=boom, watched=lambda: [],
                            near_iv=lambda s, d: None, out_dir=tmp_path)  # fmt: skip
    res = await ar.archive_step(AFTER_CLOSE, src)
    assert "gsec_spread" in res["failed"] and res["failed"]["sector_pe"] == "no rows"
    assert ar._tries[("gsec_spread", date(2026, 9, 30))][0] == 1
    again = await ar.archive_step(AFTER_CLOSE, src)  # inside the 15-minute retry gap: not tried
    assert again["failed"] == {} and ar._tries[("gsec_spread", date(2026, 9, 30))][0] == 1
