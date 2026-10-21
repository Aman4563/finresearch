"""Portfolio and holding factor exposures against the IIMA four factors (#348). Synthetic portfolios and factor series
only. Returns are built as exact linear combinations of the factors, so the expected exposures are the coefficients
used to build them (no reference to the code under test)."""

from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from finresearch.adapters.iima_factors import FactorDay, FactorSeries
from finresearch.portfolio import factor_exposures as fx
from finresearch.portfolio.history import History, Position

TRUE = {"alpha": 0.0001, "mkt": 1.2, "smb": 0.5, "hml": -0.3, "wml": 0.2}


def trading_days(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def factors(days: list[date], seed: int = 11) -> FactorSeries:
    g = random.Random(seed)
    rows = {d: FactorDay(mf=g.gauss(0, 0.01), smb=g.gauss(0, 0.006), hml=g.gauss(0, 0.006), wml=g.gauss(0, 0.007),
                         rf=0.00025) for d in days}  # fmt: skip
    return FactorSeries(release="2099-12", url="https://example.invalid/f.csv", rows=rows)


def ret(f: FactorDay, b: dict[str, float] = TRUE) -> float:
    return f.rf + b["alpha"] + b["mkt"] * f.mf + b["smb"] * f.smb + b["hml"] * f.hml + b["wml"] * f.wml


def history(days: list[date], fs: FactorSeries, b: dict[str, float] = TRUE) -> History:
    rets: list[float | None] = [None]
    for d in days[1:]:
        rets.append(ret(fs.rows[d], b) if d in fs.rows else 0.001)
    idx, v = [1.0], 1.0
    for r in rets[1:]:
        v *= 1 + (r or 0)
        idx.append(v)
    return History(
        days=days, value=[100000 * x for x in idx], returns=rets, index=idx, flow=[0.0] * len(days)
    )


def by(payload_part: dict) -> dict[str, dict]:
    return {e["factor"]: e for e in payload_part["exposures"]}


def test_portfolio_exposures_recover_the_true_loadings_and_report_window_and_source():
    days = trading_days(date(2024, 1, 1), 400)
    fs = factors(days)
    h = history(days, fs)
    out = fx.exposures(h, fs, {})
    p = out["portfolio"]
    assert p["available"] is True and p["reason"] is None
    e = by(p)
    for k in ("mkt", "smb", "hml", "wml"):
        assert e[k]["beta"] == pytest.approx(TRUE[k], abs=1e-9)
        assert e[k]["se"] is not None and e[k]["ci95"][0] <= e[k]["beta"] <= e[k]["ci95"][1]
    assert p["alpha_annual"]["value"] == pytest.approx(TRUE["alpha"] * 252, abs=1e-8)
    assert p["window"] == {"start": days[1].isoformat(), "end": days[-1].isoformat(), "returns": 399,
                           "dropped": 0}  # fmt: skip
    assert p["r2"] == pytest.approx(1.0)
    assert out["source"]["release"] == "2099-12" and out["source"]["last"] == days[-1].isoformat()
    assert "Agarwalla" in out["source"]["citation"]


def test_window_ends_where_the_factor_file_ends_and_says_so():
    days = trading_days(date(2024, 1, 1), 500)
    fs = factors(days[:380])  # the release stops before the portfolio's last day
    h = history(days, fs)
    out = fx.exposures(h, fs, {})
    p = out["portfolio"]
    assert p["window"]["end"] == days[379].isoformat()
    assert by(p)["mkt"]["beta"] == pytest.approx(1.2, abs=1e-9)
    assert days[379].isoformat() in out["coverage_note"] and days[-1].isoformat() in out["coverage_note"]


def test_intervals_that_do_not_match_a_factor_day_are_dropped_and_counted():
    days = trading_days(date(2024, 1, 1), 300)
    fs = factors(days)
    extra = date(
        2024, 3, 2
    )  # a Saturday NAV date: no factor row, so (Fri, Sat] and (Sat, Mon] cannot be matched
    pdays = sorted([*days, extra])
    h = history(pdays, fs)
    p = fx.exposures(h, fs, {})["portfolio"]
    # 301 days give 300 returns; the two around the Saturday are dropped
    assert p["window"]["dropped"] == 2 and p["window"]["returns"] == 298
    assert by(p)["mkt"]["beta"] == pytest.approx(1.2, abs=1e-9)


def test_too_short_overlap_is_a_reason_never_zero():
    days = trading_days(date(2024, 1, 1), 60)
    fs = factors(days)
    p = fx.exposures(history(days, fs), fs, {})["portfolio"]
    assert p["available"] is False and p["exposures"] == []
    assert p["reason"] == f"needs {fx.MIN_RETURNS} daily returns that overlap the factor data; have 59"


def test_no_overlap_names_both_ranges():
    fdays = trading_days(date(2020, 1, 1), 300)
    pdays = trading_days(date(2024, 1, 1), 300)
    fs = factors(fdays)
    p = fx.exposures(history(pdays, factors(pdays)), fs, {})["portfolio"]
    assert p["available"] is False and "have 0" in p["reason"]


def test_missing_factor_data_is_unavailable_with_its_reason():
    days = trading_days(date(2024, 1, 1), 300)
    out = fx.exposures(history(days, factors(days)), None, {}, source_error="IIMA data library HTTP 503")
    assert (
        out["available"] is False
        and out["reason"] == "IIMA factor data could not be read: IIMA data library HTTP 503"
    )
    assert out["portfolio"] is None and out["holdings"] == []


def test_window_is_capped_to_the_last_three_years():
    days = trading_days(date(2018, 1, 1), 1000)
    fs = factors(days)
    p = fx.exposures(history(days, fs), fs, {})["portfolio"]
    assert p["window"]["returns"] == fx.WINDOW_RETURNS == 756
    assert p["window"]["end"] == days[-1].isoformat() and p["window"]["start"] == days[-756].isoformat()


def pos(key: str, name: str, value: float) -> Position:
    return Position(key, name, "stock", "equity", [1], 10.0, value / 10, None, value, None, None)


def test_holdings_exposures_from_their_own_closes_with_reasons_for_the_rest():
    days = trading_days(date(2024, 1, 1), 300)
    fs = factors(days)
    h = history(days, fs)
    b = {"alpha": 0.0, "mkt": 0.8, "smb": -0.2, "hml": 0.6, "wml": 0.0}
    closes, px = {}, 100.0
    for d in days:
        if d != days[0]:
            px *= 1 + ret(fs.rows[d], b)
        closes[d] = px
    h.positions = [pos("NSE:AAAA", "Alpha Synthetic Ltd", 60000), pos("NSE:BBBB", "Beta Synthetic Ltd", 30000),
                   pos("MF:999", "Gamma Synthetic Fund", 10000)]  # fmt: skip
    short = {d: 50.0 + i * 0.01 for i, d in enumerate(days[-30:])}
    out = fx.exposures(h, fs, {"NSE:AAAA": closes, "NSE:BBBB": short})
    rows = {r["key"]: r for r in out["holdings"]}
    a = rows["NSE:AAAA"]
    assert a["available"] is True and a["weight"] == pytest.approx(0.6)
    assert by(a)["hml"]["beta"] == pytest.approx(0.6, abs=1e-9) and by(a)["smb"]["beta"] == pytest.approx(
        -0.2, abs=1e-9
    )
    assert rows["NSE:BBBB"]["available"] is False and "have 29" in rows["NSE:BBBB"]["reason"]
    assert (
        rows["MF:999"]["available"] is False and rows["MF:999"]["reason"] == "no price history could be read"
    )


class FakeFetch:
    """fetch(instrument, a, b) -> (closes, complete), as portfolio.history.Fetcher; one key fails."""

    def __init__(self, closes: dict[str, dict[date, float]]):
        self.closes = closes
        self.calls: list[tuple[str, date, date]] = []

    async def __call__(self, inst, a, b):
        self.calls.append((inst.key, a, b))
        if inst.key == "NSE:FAIL":
            raise RuntimeError("exchange down")
        return {d: v for d, v in self.closes.get(inst.key, {}).items() if a <= d <= b}, True


async def test_load_reads_each_holding_over_the_factor_window_and_isolates_failures(tmp_path):
    from finresearch.portfolio.history import PriceStore

    days = trading_days(date(2023, 1, 2), 500)
    fs = factors(days[:450])
    h = history(days, fs)
    b = {"alpha": 0.0, "mkt": 1.0, "smb": 0.3, "hml": 0.0, "wml": -0.4}
    closes, px = {}, 100.0
    for d in days[:450]:
        if d != days[0]:
            px *= 1 + ret(fs.rows[d], b)
        closes[d] = px
    h.positions = [
        pos("NSE:AAAA", "Alpha Synthetic Ltd", 70000),
        pos("NSE:FAIL", "Broken Synthetic Ltd", 30000),
    ]
    f = FakeFetch({"NSE:AAAA": closes})
    out = await fx.load(h, fs, f, PriceStore(tmp_path / "ph"), today=days[-1])
    rows = {r["key"]: r for r in out["holdings"]}
    assert by(rows["NSE:AAAA"])["wml"]["beta"] == pytest.approx(-0.4, abs=1e-9)
    assert (
        rows["NSE:FAIL"]["available"] is False
        and rows["NSE:FAIL"]["reason"] == "no price history could be read"
    )
    # asked for three years up to the factor file's end, not up to today
    ask = [c for c in f.calls if c[0] == "NSE:AAAA"]
    assert (
        ask
        and max(c[2] for c in ask) == fs.last
        and min(c[1] for c in ask) <= date(fs.last.year - 3, fs.last.month, fs.last.day)
    )
