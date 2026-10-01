"""Exchange disclosures (FEATURE_RESEARCH items 1-3): surveillance and pledge flags, insider / SAST / deal feeds, credit
rating actions and SEBI orders. Parsers run on synthetic fixtures with the recorded field names
(tests/fixtures/nse/disclosures/); the database tests serve them through httpx.MockTransport. No network."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from finresearch.adapters import nse_disclosures as nd
from finresearch.adapters import sebi_orders as so
from finresearch.fincalc import disclosures as fd

FIX = Path(__file__).parent / "fixtures" / "nse" / "disclosures"
SEBI_RSS = Path(__file__).parent / "fixtures" / "sebi" / "sebirss_synthetic.xml"
TODAY = date(2026, 10, 1)
NOW = datetime(2026, 10, 1, 3, 0, tzinfo=UTC)  # 08:30 IST
SYNTH_PAN = "ABCDE1234F"


def run(coro):
    return asyncio.run(coro)


def fx(name: str):
    return json.loads((FIX / name).read_text())


# --------------------------------------------------------------------------- privacy
def test_scrub_removes_pan_aadhaar_and_din():
    text = f"Order against Some Person (PAN: {SYNTH_PAN}) and Other {SYNTH_PAN.lower().upper()}; UID 1234 5678 9012; DIN: 01234567"
    out = nd.scrub(text)
    assert SYNTH_PAN not in out and "1234 5678 9012" not in out and "01234567" not in out
    assert out.startswith("Order against Some Person") and "PAN:" not in out


def test_sebi_rss_keeps_enforcement_items_and_strips_pans():
    orders, built = so.parse_rss(SEBI_RSS.read_bytes())
    assert built == datetime(2026, 10, 1, 14, 0, 2)
    assert len(orders) == 4  # the press release is dropped
    assert all(SYNTH_PAN not in o.title and "ZYXWV9876K" not in o.title for o in orders)
    assert orders[0].day == date(2026, 9, 30) and orders[0].kind == "settlement"
    assert orders[2].kind == "recovery"


def test_name_matching_guards_against_lookalike_names():
    assert so.name_in_title("Example Ltd", "Settlement Order in the matter of Example Ltd")
    assert so.name_in_title("EXAMPLE LIMITED", "Order in the matter of Example Ltd.")
    assert not so.name_in_title("Example Ltd", "Adjudication Order in the matter of Example Holdings Ltd")
    assert not so.name_in_title("Example Holdings Ltd", "Settlement Order in the matter of Example Ltd")
    # a one-word name needs a legal suffix after it; a two-word name may be followed by a joining word
    assert not so.name_in_title("Example Ltd", "Order in the matter of Example Agro and others")
    assert so.name_in_title("Sample Finance Ltd", "Order in the matter of Sample Finance in the scrip of X")
    orders, _ = so.parse_rss(SEBI_RSS.read_bytes())
    hits = so.match_orders(orders, {"EXAMPLE": "Example Ltd", "EXHOLD": "Example Holdings Ltd"})
    assert sorted((h.key, h.order.link[-11:]) for h in hits) == [("EXAMPLE", "900001.html"), ("EXAMPLE", "900003.html"),
                                                                 ("EXHOLD", "900002.html")]  # fmt: skip


# --------------------------------------------------------------------------- parsers
def test_dates_in_every_feed_format():
    assert nd.parse_day("29-09-2026") == date(2026, 9, 29)  # credit ratings
    assert nd.parse_day("30-SEP-2026") == date(2026, 9, 30)  # deals (upper-case month)
    assert nd.parse_day("23-SEP-2026 to 25-SEP-2026") == date(2026, 9, 23)  # SAST range: first day
    assert nd.parse_day("2026-09-30") == date(2026, 9, 30)  # XBRL
    assert nd.parse_day("30 Sep, 2026 +0530") == date(2026, 9, 30)  # SEBI RSS
    assert nd.parse_day("01-OCT-2026 11:57:55") == date(2026, 10, 1)
    assert nd.parse_day("-") is None and nd.parse_day("") is None and nd.parse_day(None) is None
    assert nd.parse_when("01-OCT-2026 11:57:55").utcoffset() == timedelta(hours=5, minutes=30)


def test_surveillance_codes_are_parsed_not_taken_from_gsm_stage():
    pc = nd.parse_surv_code("IBC - Receipt & GSM 0 (62)")
    assert pc["gsm"] == "0" and pc["ibc"] and pc["components"] == ["IBC Receipt", "GSM 0"]
    assert nd.parse_surv_code("ESM II & GSM 0 (37)")["esm"] == "II"
    assert nd.parse_surv_code("GSM IV & IBC - Receipt (66)")["gsm"] == "IV"
    gsm = {r.symbol: r for r in nd.parse_gsm(fx("gsm.json"))}
    assert (
        gsm["TESTPOWER"].stage == "0" and gsm["TESTPOWER"].ibc
    )  # gsmStage "LXII" is the code number, not a stage
    assert gsm["MOCKAGRO"].label == "GSM Stage 0 · ESM Stage II"
    asm = {r.symbol: r for r in nd.parse_asm(fx("asm.json"))}
    assert asm["EXAMPLE"].term == "long-term" and asm["EXAMPLE"].stage == "I"
    assert asm["DEMOTECH"].label == "ASM short-term Stage II"


def test_fno_ban_needs_its_header():
    ban = nd.parse_fno_ban((FIX / "fo_secban.csv").read_text())
    assert ban.trade_date == date(2026, 10, 1) and ban.symbols == ["EXAMPLE", "SAMPLEFIN"]
    with pytest.raises(nd.NseError, match="not published"):
        nd.parse_fno_ban("<html><body>NSE</body></html>")
    assert nd.parse_fno_ban("Securities in Ban For Trade Date 02-OCT-2026:\n").symbols == []  # a real "none"


def test_pledge_is_recomputed_from_share_counts():
    p = nd.parse_pledge(fx("pledge_EXAMPLE.json"), "example")
    # 18,000,000 encumbered / 120,000,000 promoter shares = 15 %; / 200,000,000 issued = 9 %; promoter 60 %
    assert p.symbol == "EXAMPLE" and p.quarter_end == date(2026, 6, 30)
    assert p.pct_of_promoter == Decimal("15.0000") and p.pct_of_equity == Decimal("9.0000")
    assert p.promoter_pct == Decimal("60.0000") and p.mismatch is None
    assert p.reported_pct_of_promoter == Decimal("15.00")  # padded "    15.00" parsed
    b = nd.parse_pledge(fx("pledge_EXAMPLE_blank.json"), "EXAMPLE")
    assert b.reported_pct_of_promoter is None and b.pct_of_promoter == Decimal(
        "10.0000"
    )  # blank: still recomputed
    assert b.disclosed_shares is None  # "-"
    bad = fx("pledge_EXAMPLE.json")
    bad["data"][0]["percPromoterShares"] = "16.00"
    assert "recomputed 15.00 vs NSE 16.00" in nd.parse_pledge(bad, "EXAMPLE").mismatch
    assert nd.parse_pledge({"data": []}, "EXAMPLE") is None


def test_pledge_change_needs_two_quarters():
    assert (
        fd.pledge_change([(date(2026, 6, 30), Decimal("15"))])[0] is None
    )  # first snapshot: unknown, not 0 pp
    ch, q0, q1 = fd.pledge_change([(date(2026, 6, 30), Decimal("15.0000")), (date(2026, 3, 31), Decimal("10.0000")),
                                   (date(2025, 12, 31), None)])  # fmt: skip
    assert (ch, q0, q1) == (Decimal("5.0000"), date(2026, 3, 31), date(2026, 6, 30))


def test_pit_xbrl_rows_and_net_insider_value_by_hand():
    rows = [t.model_dump() for t in nd.parse_pit_xbrl((FIX / "pit_filing_9001.xml").read_bytes())]
    rows += [t.model_dump() for t in nd.parse_pit_xbrl((FIX / "pit_filing_9002.xml").read_bytes())]
    assert len(rows) == 6 and rows[0]["symbol"] == "EXAMPLE" and rows[0]["isin"] == "INE000X01011"
    assert rows[0]["value_inr"] == Decimal("50000000") and rows[0]["mode"] == "Market Purchase"
    net = fd.net_insider(rows, TODAY, 90)  # window 04-Jul-2026 .. 01-Oct-2026, both included
    assert net.start == date(2026, 7, 4)
    # counted: promoter buy 5,00,00,000 (18-Sep) + promoter buy 9,00,000 (04-Jul, first day of the window)
    #          director sell 1,10,00,000 (25-Sep). Not counted: ESOP, inter-se transfer; 03-Jul is outside.
    assert net.buy_value == Decimal("50900000") and net.sell_value == Decimal("11000000")
    assert net.net_value == Decimal("39900000") and net.n_counted == 3
    assert net.by_group == {
        "promoter": Decimal("50900000"),
        "director_kmp": Decimal("-11000000"),
        "other": Decimal(0),
    }
    assert dict(net.excluded) == {"ESOP / employee allotment": 1, "inter-se transfer": 1}


def test_insider_classification_rules():
    ok = {"instrument": "Equity", "mode": "Market Purchase", "side": "Buy"}
    assert fd.classify(ok) == ("buy", None)
    assert fd.classify({**ok, "side": "Sell"}) == (None, "side disagrees with mode")
    assert fd.classify({**ok, "instrument": "Warrants"})[0] is None
    assert fd.classify({**ok, "mode": "Gift"}) == (None, "gift")
    assert fd.classify({**ok, "mode": "Off Market"}) == (None, "off-market transfer")
    assert (
        fd.classify({**ok, "mode": "Revokation of Pledge", "side": "Pledge Revoke"})[1]
        == "pledge creation / release"
    )
    assert fd.person_group("Key Managerial Personnel") == "director_kmp"
    assert fd.person_group("Immediate relative") == "other"


def test_sast_and_deals():
    s = {x.acquirer: x for x in nd.parse_sast(fx("sast_EXAMPLE.json"))}
    a = s["Sample Capital Fund"]
    assert a.day == date(2026, 9, 22) and a.shares == Decimal("10500000") and a.pct == Decimal("5.25")
    assert a.promoter is False and a.regulation == "Reg29(1)"
    sale = s["Example Promoter Holdings Pvt Ltd"]
    assert sale.kind == "Sale" and sale.shares == Decimal("4000000") and sale.pct == Decimal("2")
    d = nd.parse_deals(fx("bulk_EXAMPLE.json"), "bulk")
    assert (
        d[0].day == date(2026, 9, 29)
        and d[0].side == "BUY"
        and d[0].value_inr == Decimal("1200000") * Decimal("512.35")
    )
    assert nd.parse_deals(fx("block_EXAMPLE.json"), "block")[0].remarks is None


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (("Downgrade", None, "CARE A", None, "CARE", "CARE AA", None, "CARE"), ("downgrade", True)),
        (
            (
                "Reaffirm",
                None,
                "CARE AA-; Negative",
                "Negative",
                "CARE Ratings",
                "CARE AA; Stable",
                "Stable",
                "CARE Ratings",
            ),
            ("downgrade", True),
        ),  # a bare "Reaffirm" that moved a notch at the same agency
        (
            ("Reaffirm", None, "IND AAA", "Stable", "India Ratings", "CARE AAA", "Stable", "CARE Ratings"),
            ("reaffirm", False),
        ),  # another agency's earlier rating is not compared
        (
            (
                "Reaffirm",
                None,
                "CRISIL AA; Negative",
                "Negative",
                "CRISIL",
                "CRISIL AA; Stable",
                "Stable",
                "CRISIL",
            ),
            ("outlook_negative", True),
        ),
        (("Other", "Withdrawn", None, None, "IND", "IND AA+/Stable", None, "IND"), ("withdrawn", False)),
        (
            ("Other", "Assigned", "IND A-/Stable/IND A2+", None, "IND", "IND A-/Stable", None, "IND"),
            ("new", False),
        ),
        (("New", None, "Crisil A1+", None, "CRISIL", None, None, None), ("new", False)),
        (("Other", None, "CARE D", None, "CARE", "CARE BB", None, "CARE"), ("default", True)),
        (
            (
                "Other",
                "Placed on watch",
                "[ICRA]BBB (Rating Watch with Negative Implications)",
                None,
                "ICRA",
                "[ICRA]BBB; Stable",
                "Stable",
                "ICRA",
            ),
            ("watch_negative", True),
        ),
        (
            ("Reaffirm", None, "CARE BB; Issuer Not Cooperating", None, "CARE", "CARE BB", None, "CARE"),
            ("not_cooperating", True),
        ),
        (
            ("Upgrade", None, "CRISIL AA+", "Stable", "CRISIL", "CRISIL AA", "Stable", "CRISIL"),
            ("upgrade", False),
        ),
    ],
)
def test_rating_action_mapping(args, expected):
    kind, adverse, _ = nd.classify_rating(*args)
    assert (kind, adverse) == expected


def test_rating_notches_and_issuer_code():
    assert nd.rating_notch("IND A-/Stable/IND A2+") == ("long", nd.LONG_SCALE.index("A-"))
    assert nd.rating_notch("Crisil A1+") == ("short", 0)
    assert nd.rating_notch("[ICRA]BBB (Rating Watch with Negative Implications)") == (
        "long",
        nd.LONG_SCALE.index("BBB"),
    )
    assert nd.issuer_code("INE000X07018") == "INE000X" == nd.issuer_code("INE000X01011")
    assert nd.issuer_code("INF000X01011") is None and nd.issuer_code("bad") is None
    rows = nd.parse_credit_ratings(fx("credit_rating.json"))
    ex = rows[0]
    assert (
        ex.action == "downgrade"
        and ex.adverse
        and ex.outlook == "Negative"
        and ex.rating_day == date(2026, 9, 27)
    )
    assert ex.derived and "same agency" in ex.derived
    assert rows[1].action == "withdrawn" and rows[1].symbol is None


# --------------------------------------------------------------------------- the client over a fake NSE
class FakeNse:
    """Serves the fixtures by URL; `down` makes every request fail at the transport level."""

    def __init__(self):
        self.down = False
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(url)
        if self.down:
            raise httpx.ConnectError("DNS failure", request=request)
        path, q = request.url.path, dict(request.url.params)
        if (
            not path.startswith("/api/")
            and not path.startswith("/content")
            and not path.startswith("/corporate")
        ):
            return httpx.Response(200, text="<html>NSE</html>", headers={"content-type": "text/html"})
        body = {
            "/api/reportASM": lambda: fx("asm.json"),
            "/api/reportGSM": lambda: fx("gsm.json"),
            "/api/corporate-credit-rating": lambda: fx("credit_rating.json"),
            "/api/corporate-pledgedata": lambda: fx("pledge_EXAMPLE.json") if q.get("symbol") == "EXAMPLE" else {"data": []},
            "/api/corporate-sast-reg29": lambda: fx("sast_EXAMPLE.json") if q.get("symbol") == "EXAMPLE" else {"data": []},
            "/api/corporates-pit-gg": lambda: fx("pit_index_EXAMPLE.json") if q.get("symbol") == "EXAMPLE" else {"data": []},
            "/api/historicalOR/bulk-block-short-deals": lambda: (fx("bulk_EXAMPLE.json") if q.get("optionType") ==
                                                                 "bulk_deals" else fx("block_EXAMPLE.json"))
            if q.get("symbol") == "EXAMPLE" else {"data": []},
        }.get(path)  # fmt: skip
        if body is not None:
            return httpx.Response(200, json=body())
        if path == "/content/fo/fo_secban.csv":
            return httpx.Response(
                200, text=(FIX / "fo_secban.csv").read_text(), headers={"content-type": "text/csv"}
            )
        if path.startswith("/corporate/xbrl/IT_9001"):
            return httpx.Response(200, content=(FIX / "pit_filing_9001.xml").read_bytes())
        if path.startswith("/corporate/xbrl/IT_9002"):
            return httpx.Response(200, content=(FIX / "pit_filing_9002.xml").read_bytes())
        return httpx.Response(404, text="not found")


def client_for(net: FakeNse, tmp_path):
    from finresearch.adapters.http import PoliteClient
    from finresearch.adapters.nse import NseClient

    async def no_sleep(_):
        return None

    def make():
        http = PoliteClient(
            transport=httpx.MockTransport(net), cache_dir=tmp_path / "http", sleep=no_sleep, max_retries=0
        )
        return nd.NseDisclosures(NseClient(http, warmup_url=nd.INSIDER_PAGE))

    return make


def test_client_reads_every_feed(tmp_path):
    net = FakeNse()

    async def go():
        async with client_for(net, tmp_path)() as d:
            asm, ban = await d.asm(), await d.fno_ban()
            pit = await d.pit_index("EXAMPLE", date(2026, 7, 4), TODAY)
            txns = await d.pit_filing(pit.items[0].xml_url)
            with pytest.raises(nd.NseError, match="outside NSE's archive"):
                await d.pit_filing("https://example.com/x.xml")
            return asm, ban, pit, txns

    asm, ban, pit, txns = run(go())
    assert len(asm.items) == 3 and ban.items.trade_date == date(2026, 10, 1)
    assert (
        pit.url.startswith("https://www.nseindia.com/api/corporates-pit-gg?")
        and "from_date=04-07-2026" in pit.url
    )
    assert [t.mode for t in txns] == ["Market Purchase", "ESOP"]


def test_oversize_and_block_pages_raise(tmp_path):
    from finresearch.adapters.http import PoliteClient
    from finresearch.adapters.nse import NseClient

    def handler(request):
        if request.url.path == "/api/reportGSM":
            return httpx.Response(200, json=[{"symbol": "X" * 50}] * 200_000)
        if request.url.path.startswith("/api/"):
            return httpx.Response(
                200, text="<html>Access Denied</html>", headers={"content-type": "text/html"}
            )
        return httpx.Response(200, text="<html></html>")

    async def no_sleep(_):
        return None

    async def go():
        http = PoliteClient(
            transport=httpx.MockTransport(handler), cache_dir=tmp_path, sleep=no_sleep, max_retries=0
        )
        async with nd.NseDisclosures(NseClient(http)) as d:
            with pytest.raises(nd.NseError, match="cap"):
                await d.gsm()
            with pytest.raises(nd.NseError, match="non-JSON or block page"):
                await d.asm()

    run(go())


# --------------------------------------------------------------------------- database: refresh, views, alerts
@pytest.fixture
def disc(env, tmp_path, monkeypatch):
    from finresearch.db import session_scope
    from finresearch.db.models import (
        AlertEvalSlot,
        Company,
        DisclosureFeed,
        DisclosureRecord,
        PortfolioHolding,
        PortfolioLot,
        Watch,
    )
    from finresearch.disclosures import alerts as dal
    from finresearch.disclosures import refresh

    with session_scope() as s:
        for m in (DisclosureRecord, DisclosureFeed, AlertEvalSlot, PortfolioLot, PortfolioHolding, Watch):
            s.query(m).delete()
        s.query(Company).filter(Company.slug.in_(("disc-example", "bond-ine000y08012"))).delete(
            synchronize_session=False
        )
        co = Company(
            slug="disc-example", name="Example Ltd", nse_symbol="EXAMPLE", isin="INE000X01011", meta={}
        )
        s.add(co)
        s.flush()
        s.add(
            Watch(company_id=co.id, kind="stock", nse_symbol="EXAMPLE", exchange="NSE", meta={}, active=True)
        )
        s.add(Company(slug="bond-ine000y08012", name="Sample Finance NCD", meta={}))
    net = FakeNse()

    async def sebi():
        orders, built = so.parse_rss(SEBI_RSS.read_bytes())
        return orders, built, None

    monkeypatch.setattr(refresh, "SOURCES", refresh.Sources(client=client_for(net, tmp_path), sebi=sebi))
    monkeypatch.setattr(dal, "NOW", lambda: NOW)
    return net


def _refresh_all(now=NOW):
    from finresearch.disclosures import refresh

    m = run(refresh.refresh_market(now))
    st = run(refresh.refresh_stock("EXAMPLE", now, isin="INE000X01011"))
    return m, st


def test_refresh_then_stock_view_with_sources_and_as_of(disc):
    from finresearch.db import session_scope
    from finresearch.disclosures import views

    m, st = _refresh_all()
    assert set(m.values()) == {"ok"} and set(st.values()) == {"ok"}
    with session_scope() as s:
        v = views.stock(s, "EXAMPLE", NOW)
    labels = {f["kind"]: f for f in v["flags"]}
    assert labels["asm"]["label"] == "ASM long-term Stage I" and labels["asm"]["as_of"] == "2026-10-01"
    assert labels["asm"]["source_url"] == "https://www.nseindia.com/api/reportASM"
    assert labels["fno_ban"]["label"] == "F&O ban (2026-10-01)"
    assert (
        labels["pledge"]["label"] == "Promoter pledge 15.00% of holding"
        and labels["pledge"]["tone"] == "info"
    )
    assert v["stage"] == "ASM long-term Stage I" and v["unavailable"] == []
    ins = v["insider"]
    assert ins["complete"] and ins["net"]["net_value"] == "39900000" and ins["state"] == "ok"
    assert {t["excluded_why"] for t in ins["trades"] if not t["counted"]} == {
        "ESOP / employee allotment",
        "inter-se transfer",
    }
    assert len(v["deals"]["rows"]) == 3 and v["deals"]["recent_n"] == 2  # 29-Sep and 30-Sep within 5 days
    r = v["ratings"]
    assert r["issuer_code"] == "INE000X" and r["latest_adverse"]["action"] == "downgrade"
    assert r["actions"][0]["scope"] == "same issuer"
    titles = [o["title"] for o in v["sebi"]["orders"]]
    assert len(titles) == 2 and all(SYNTH_PAN not in t for t in titles)


def test_refetch_is_idempotent_and_pledge_history_gives_the_change(disc):
    from finresearch.db import session_scope
    from finresearch.db.models import DisclosureRecord
    from finresearch.disclosures import store, views

    _refresh_all()
    with session_scope() as s:
        n = s.query(DisclosureRecord).count()
        assert views.pledge(s, "EXAMPLE", NOW)["change_pp"] is None  # one quarter only
    _refresh_all(NOW + timedelta(hours=1))
    with session_scope() as s:
        assert s.query(DisclosureRecord).count() == n  # nothing duplicated
        # an earlier quarter recorded (as a previous pass would have): 10 % -> 15 % = +5 pp
        store.upsert_records(s, "pledge", [{"symbol": "EXAMPLE", "day": date(2026, 3, 31), "key": ("EXAMPLE", date(2026, 3, 31)),
                                            "data": nd.parse_pledge(fx("pledge_EXAMPLE_blank.json"), "EXAMPLE")}], update=True)  # fmt: skip
        p = views.pledge(s, "EXAMPLE", NOW)
        assert p["change_pp"] == "5.0000" and p["prev_quarter"] == "2026-03-31"
        flag = next(
            f
            for f in views.flags_of("EXAMPLE", views.surveillance(s, "EXAMPLE", NOW), p)
            if f["kind"] == "pledge"
        )
        assert flag["tone"] == "warn" and "+5.00 pp" in flag["label"]


def test_sebi_feed_stores_only_matched_orders_without_pans(disc):
    from finresearch.db import session_scope
    from finresearch.db.models import DisclosureFeed, DisclosureRecord

    _refresh_all()
    with session_scope() as s:
        dump = json.dumps(
            [r.data for r in s.query(DisclosureRecord).all()]
            + [f.payload for f in s.query(DisclosureFeed).all()]
        )
        orders = s.query(DisclosureRecord).filter(DisclosureRecord.dataset == "sebi_order").all()
    assert SYNTH_PAN not in dump and "ZYXWV9876K" not in dump and "Unrelated Person" not in dump
    assert sorted(o.symbol for o in orders) == ["EXAMPLE", "EXAMPLE"]


def test_failure_reads_unavailable_never_none(disc):
    from finresearch.db import session_scope
    from finresearch.disclosures import refresh, views

    disc.down = True
    m = run(refresh.refresh_market(NOW, ("asm", "gsm", "fno_ban")))
    st = run(refresh.refresh_stock("EXAMPLE", NOW, datasets=("pledge", "pit")))
    assert all("ConnectError" in v for v in [*m.values(), *st.values()])
    with session_scope() as s:
        v = views.stock(s, "EXAMPLE", NOW)
    assert v["flags"] == [] and v["stage"] is None  # no flag shown, but not "none" either:
    assert (
        v["surveillance"]["asm"]["state"] == "unavailable"
        and "ConnectError" in v["surveillance"]["asm"]["reason"]
    )
    assert v["surveillance"]["fno_ban"]["in_ban"] is None
    assert v["insider"]["net"] is None and not v["insider"]["complete"]
    assert "NSE ASM list" in v["unavailable"] and "promoter pledge (NSE)" in v["unavailable"]


def test_last_good_read_survives_a_failure_until_it_is_too_old(disc):
    from finresearch.db import session_scope
    from finresearch.disclosures import refresh, views

    _refresh_all()
    disc.down = True
    later = NOW + timedelta(hours=6)
    run(refresh.refresh_market(later, ("asm",)))
    with session_scope() as s:
        st = views.surveillance(s, "EXAMPLE", later)["asm"]
        assert st["state"] == "ok" and "ConnectError" in st["error"] and st["entries"]
        old = views.surveillance(s, "EXAMPLE", NOW + timedelta(days=5))
        assert old["asm"]["state"] == "unavailable" and old["asm"]["entries"]  # kept, flagged stale
        assert views.flags_of("EXAMPLE", old, {"latest": None})[0]["stale"]
        # the ban list read is for 01-Oct: on 02-Oct it no longer says anything about today
        assert old["fno_ban"]["in_ban"] is None


def test_incomplete_insider_window_is_unknown(disc, monkeypatch):
    from finresearch.alerts.compute import Reader
    from finresearch.db import session_scope
    from finresearch.disclosures import refresh

    monkeypatch.setattr(refresh, "MAX_FILINGS", 1)  # one of the two filings read this pass
    _refresh_all()
    with session_scope() as s:
        r = run(Reader(s).read("stock", "insider_net_buy_90d", "EXAMPLE", {}, {}))
    assert r.value is None and "not read yet" in r.note
    monkeypatch.setattr(refresh, "MAX_FILINGS", 25)
    run(refresh.refresh_stock("EXAMPLE", NOW, datasets=("pit",)))
    with session_scope() as s:
        r = run(Reader(s).read("stock", "insider_net_buy_90d", "EXAMPLE", {}, {}))
    assert r.value == Decimal("39900000")


def test_alert_metrics_from_stored_disclosures(disc):
    from finresearch.alerts.compute import Reader
    from finresearch.db import session_scope

    _refresh_all()
    with session_scope() as s:
        rd = Reader(s)
        val = lambda m, inst="EXAMPLE", kind="stock", b=None: run(rd.read(kind, m, inst, {}, b or {}))  # noqa: E731
        assert val("pledge_pct").value == Decimal("15.0000")
        assert val("pledge_change_pp").value is None  # one quarter recorded
        assert val("in_fno_ban").value == 1 and val("in_ban", "EXAMPLE", "fno").value == 1
        assert val("in_ban", "NIFTY", "fno").value == 0
        assert val("bulk_block_deals_5d").value == 2
        first = val("rating_action")  # first check: a downgrade 4 days old fires
        assert first.value == 1 and "downgrade" in first.detail and first.baseline["rating"]
        assert val("rating_action", b=first.baseline).value == 0  # nothing new since
        assert val("sebi_order").value == 1
        bond = val("rating_action", "INE000Y08012", "bond")
        assert bond.value == 0 and bond.baseline == {"rating": None}  # withdrawn is not adverse
        assert val("pledge_pct", "BSE:999999").value is None


def test_surveillance_stage_change_fires_once_then_clears(disc):
    from finresearch.alerts.compute import Reader
    from finresearch.db import session_scope
    from finresearch.disclosures import store

    _refresh_all()

    def read(baseline):
        with session_scope() as s:
            return run(Reader(s).read("stock", "surveillance_stage", "EXAMPLE", {}, baseline))

    r0 = read({})
    assert r0.value == 0 and r0.baseline == {"stage": "ASM long-term Stage I"}  # first check records
    with session_scope() as s:  # NSE moves it off ASM and onto GSM 0 with an IBC marker
        f = store.feed(s, "asm")
        f.payload = {"rows": [r for r in f.payload["rows"] if r["symbol"] != "EXAMPLE"]}
        g = store.feed(s, "gsm")
        g.payload = {
            "rows": [
                *g.payload["rows"],
                {**g.payload["rows"][0], "symbol": "EXAMPLE", "isin": "INE000X01011"},
            ]
        }
    r1 = read(r0.baseline)
    assert r1.value == 1 and r1.detail == "ASM long-term Stage I -> GSM Stage 0 · IBC (insolvency)"
    r2 = read(r1.baseline)
    assert r2.value == 0  # cleared: the engine fires again only on the next change


def test_holdings_red_flags_and_brief_section(disc):
    from finresearch.alerts.compute import Reader
    from finresearch.db import session_scope
    from finresearch.db.models import PortfolioHolding, PortfolioLot
    from finresearch.disclosures import views
    from finresearch.monitor.digest import brief_text, build_brief

    _refresh_all()
    with session_scope() as s:
        h = PortfolioHolding(ikey="ISIN:INE000X01011", account="Manual", asset_type="stock", name="Example Ltd",
                             isin="INE000X01011", nse_symbol="EXAMPLE", meta={})  # fmt: skip
        s.add(h)
        s.flush()
        s.add(PortfolioLot(holding_id=h.id, origin="buy", quantity=Decimal(10), open_quantity=Decimal(10)))
    with session_scope() as s:
        r = run(Reader(s).read("portfolio", "holdings_red_flags", "PORTFOLIO", {}, {}))
        assert r.value == 1  # one held stock flagged (ASM + F&O ban; a flat pledge is not a red flag)
        assert "EXAMPLE" in r.detail
        b = views.brief_section(s, NOW)
        assert {f["kind"] for f in b["flags"]} == {"asm", "fno_ban", "pledge"}
        assert b["insider"][0]["net_value"] == "39900000" and b["ratings"][0]["action"] == "downgrade"
        assert len(b["deals"]) == 2 and b["unavailable"] == []
        body = build_brief(s, NOW)
    assert body["disclosures"]["flags"] and "red flag" in body["headline"]
    assert "Red flag EXAMPLE: ASM long-term Stage I" in brief_text(body)


def test_api_routes(disc):
    from fastapi.testclient import TestClient

    from finresearch.api.app import create_app

    _refresh_all()
    app = create_app(clock=lambda: NOW) if "clock" in create_app.__code__.co_varnames else create_app()
    with TestClient(app) as c:
        v = c.get("/api/stocks/EXAMPLE/disclosures", params={"refresh": False}).json()
        assert v["covered"] and v["isin"] == "INE000X01011" and v["flags"]
        assert c.get("/api/stocks/BSE:999999/disclosures").json()["covered"] is False
        assert c.get("/api/stocks/EXAMPLE/disclosures", params={"isin": "nope"}).status_code == 422
        b = c.get("/api/bonds/INE000Y08012/rating-actions", params={"refresh": False}).json()
        assert (
            b["ratings"]["actions"][0]["action"] == "withdrawn"
            and b["ratings"]["actions"][0]["scope"] == "this instrument"
        )
        t = c.get("/api/disclosures/tracked").json()
        assert [x["key"] for x in t["stocks"]] == ["EXAMPLE"]
        mine = next(
            b for b in t["bonds"] if b["isin"] == "INE000Y08012"
        )  # other tests may leave bonds behind
        assert mine["tracked"] and mine["latest"]["action"] == "withdrawn"
        assert set(c.get("/api/disclosures/status").json()) == {
            "asm",
            "gsm",
            "fno_ban",
            "credit_ratings",
            "sebi_orders",
        }


def test_page_view_refreshes_only_stale_feeds(disc):
    from finresearch.disclosures import refresh

    out = run(refresh.ensure_fresh("EXAMPLE", NOW))
    assert set(out["market"]) == set(refresh.MARKET_DATASETS) and set(out["stock"]) == set(
        refresh.STOCK_DATASETS
    )
    n = len(disc.calls)
    assert run(refresh.ensure_fresh("EXAMPLE", NOW + timedelta(hours=1))) == {}  # fresh: no request
    assert len(disc.calls) == n
    disc.down = True
    run(refresh.ensure_fresh("OTHER", NOW + timedelta(hours=1)))  # a new stock: its feeds fail
    n = len(disc.calls)
    run(refresh.ensure_fresh("OTHER", NOW + timedelta(hours=1, seconds=10)))  # within the 30 s negative cache
    assert len(disc.calls) == n
    run(refresh.ensure_fresh("OTHER", NOW + timedelta(hours=1, seconds=10), retry=True))  # Retry skips it
    assert len(disc.calls) > n


def test_monitor_passes_and_retry_reads_only_what_failed(disc):
    from finresearch.monitor import disclosures as md

    ist = lambda h, m: datetime(2026, 10, 1, h, m, tzinfo=UTC) - timedelta(hours=5, minutes=30)  # noqa: E731
    assert md.due_passes(ist(7, 59)) == []
    assert md.due_passes(ist(8, 5)) == [("morning", "disclosures:morning:2026-10-01")]
    assert md.due_passes(ist(19, 31)) == [("evening", "disclosures:evening:2026-10-01")]
    assert md.due_passes(datetime(2026, 10, 3, 14, 30, tzinfo=UTC)) == []  # Saturday
    disc.down = True
    res = run(md.disclosures_step(ist(19, 31), spacing_s=0))
    assert res["evening"]["market"]["asm"].startswith("ConnectError")
    assert run(md.disclosures_step(ist(19, 40), spacing_s=0)) == {}  # retry not due yet
    disc.down = False
    disc.calls.clear()
    res = run(md.disclosures_step(ist(20, 15), spacing_s=0))
    assert set(res["evening"]["market"].values()) == {"ok"} and res["evening"]["stocks_failed"] == {}
    assert run(md.disclosures_step(ist(21, 0), spacing_s=0)) == {}  # done for the day
