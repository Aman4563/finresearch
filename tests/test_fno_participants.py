"""NSE participant-wise F&O open interest (#317): the parser on NSE's real file for 09-Oct-2026 (public aggregate data),
its checks, the store and the nightly step. No network: respx answers every request."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import text

from finresearch.adapters import nse_participant_oi as po
from finresearch.adapters.http import PoliteClient

FIXTURE = (Path(__file__).parent / "fixtures" / "nse" / "fno" / "fao_participant_oi_09102026.csv").read_text()
DAY = date(2026, 10, 9)  # a Friday


async def _no_sleep(_s: float) -> None:
    return None


def test_parses_nse_file_exactly():
    got = po.parse(FIXTURE)
    assert got.day == DAY
    assert set(got.rows) == {"Client", "DII", "FII", "Pro"}
    fii = got.rows["FII"]
    # values as printed in the file (row "FII")
    assert fii["fut_idx_long"] == 34943 and fii["fut_idx_short"] == 328931
    assert fii["opt_idx_put_long"] == 1245164 and fii["opt_idx_call_short"] == 946852
    assert fii["total_long"] == 5936804 and fii["total_short"] == 5382197
    assert got.rows["Client"]["fut_stk_short"] == 185348  # the padded header "Future Stock Short       "
    assert got.rows["Pro"]["total_long"] == 5437293  # the padded header "Total Long Contracts      "
    # NSE's own TOTAL for index-call longs is one contract above the participants' sum: kept, with a note
    assert got.notes == ("NSE's TOTAL opt_idx_call_long differs from the participants' sum by +1 contracts",)


def test_totals_that_do_not_add_up_are_refused():
    bad = FIXTURE.replace("FII,34943,", "FII,35943,")  # 1,000 contracts off
    with pytest.raises(po.ParticipantFileError, match="add up"):
        po.parse(bad)
    with pytest.raises(po.ParticipantFileError, match="header"):
        po.parse(FIXTURE.replace("Client Type", "Category"))
    with pytest.raises(po.ParticipantFileError, match="whole number"):
        po.parse(FIXTURE.replace("DII,48096", "DII,48096x"))
    with pytest.raises(po.ParticipantFileError, match="missing"):
        po.parse("\n".join(ln for ln in FIXTURE.splitlines() if not ln.startswith("Pro,")))


def test_net_positions():
    from finresearch.fnohist.participants import net_positions

    fii = po.parse(FIXTURE).rows["FII"]
    n = net_positions(fii)
    assert n["index_futures"] == 34943 - 328931
    assert n["index_puts"] == 1245164 - 704672
    assert n["index_calls"] == 600034 - 946852


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    def wipe():
        with session_scope() as s:
            s.execute(text("TRUNCATE fno_participant_oi"))
            s.execute(text("DELETE FROM alert_eval_slot WHERE slot LIKE 'fno_participant:%'"))

    wipe()
    yield env
    wipe()


@respx.mock
async def test_nightly_step_stores_the_day_and_latest_reads_changes(clean):
    from finresearch.db import session_scope
    from finresearch.db.models import AlertEvalSlot
    from finresearch.fnohist import participants
    from finresearch.monitor import fno_bhav as job

    prev = date(2026, 10, 8)
    prev_text = FIXTURE.replace("Oct 09, 2026", "Oct 08, 2026").replace(
        "FII,34943,328931", "FII,35943,327931"
    )
    prev_text = prev_text.replace("Client,291627,62104", "Client,290627,63104")
    respx.get(po.URL.format(day=DAY)).mock(return_value=httpx.Response(200, text=FIXTURE))
    respx.get(po.URL.format(day=prev)).mock(return_value=httpx.Response(200, text=prev_text))
    respx.get(url__startswith="https://nsearchives.nseindia.com/").mock(return_value=httpx.Response(404))
    now = datetime(2026, 10, 9, 14, 15, tzinfo=UTC)  # 19:45 IST, Friday
    async with PoliteClient(cache_dir=None, sleep=_no_sleep, clock=lambda: 0.0, max_retries=0) as http:
        res = await job.fno_participant_step(now, holidays=set(), http=http)
        assert res["fno_participant"]["loaded"] == 2 and not res["fno_participant"]["failed"]
        assert await job.fno_participant_step(now, holidays=set(), http=http) == {}  # claimed once a night
    with session_scope() as s:
        assert s.get(AlertEvalSlot, "fno_participant:2026-10-09").result["status"] == "done"
        got = participants.latest(s)
    assert got["day"] == "2026-10-09" and got["previous_day"] == "2026-10-08"
    fii = next(r for r in got["rows"] if r["participant"] == "FII")
    # net index futures moved from 35943 − 327931 to 34943 − 328931: −2,000 contracts
    assert fii["net_change"]["index_futures"] == -2000
    assert got["source"] == po.URL.format(day=DAY)


@respx.mock
async def test_block_page_is_an_error_not_no_file(clean):
    from finresearch.fnohist import participants

    respx.get(url__startswith="https://nsearchives.nseindia.com/").mock(
        return_value=httpx.Response(200, text="<html>Access Denied</html>")
    )
    async with PoliteClient(cache_dir=None, sleep=_no_sleep, clock=lambda: 0.0, max_retries=0) as http:
        res = await participants.update(DAY, http=http, lookback=3)
    assert res["loaded"] == 0 and res["no_file"] == 0 and len(res["failed"]) == 2 and "stopped" in res
