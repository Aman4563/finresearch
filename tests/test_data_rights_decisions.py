"""The owner's data-rights decisions of 23-Oct-2026 (#471): FBIL and NSE's combined-OI and ban files moved from PENDING
to accept-personal, and the datasets already in use without a decision (NSE's Nifty 50 members for attribution, IIMA's
factors, MoSPI's CPI) recorded and gated: each adapter asks `data_rights.require` before its first request, so without
a decision it refuses and requests nothing. Offline: httpx.MockTransport counts every request."""

from __future__ import annotations

import re
from datetime import date
from types import SimpleNamespace

import httpx
import pytest

from finresearch import data_rights
from finresearch.adapters import iima_factors as iima
from finresearch.adapters import mospi_cpi as mc
from finresearch.adapters import nse_index_weights as nw
from finresearch.adapters.http import PoliteClient

# fbil_rates was decided on 23-Oct-2026 too, then paused the same day under FBIL's Terms of Use of 13-Oct-2026 (#476)
DECIDED_1023 = ("nse_combineoi", "nse_secban", nw.DATASET, iima.DATASET, mc.DATASET)


async def _no_sleep(_: float) -> None:
    return None


def counting_client(calls: list[str]) -> PoliteClient:
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        return httpx.Response(503)

    return PoliteClient(cache_dir=None, sleep=_no_sleep, clock=lambda: 0.0, max_retries=0,
                        transport=httpx.MockTransport(handler), slots=None)  # fmt: skip


def test_the_owners_23_oct_decisions_are_recorded():
    # none of the 23-Oct datasets is still pending; PENDING stays the mechanism for later ones (fii_stats, #371)
    assert not set(DECIDED_1023) & set(data_rights.PENDING)
    for k in DECIDED_1023:
        d = data_rights.require(k)
        assert (d.dataset, d.choice, d.decided, d.ref) == (k, "accept-personal", "2026-10-23", "issue #471")
        assert d.terms_read and "Owner decision needed" not in d.terms_read
    assert "citation" in data_rights.DECISIONS[iima.DATASET].terms_read
    assert "token" in data_rights.DECISIONS[mc.DATASET].terms_read


def test_fbil_is_paused_under_its_13_oct_terms_of_use():
    """#476: FBIL's Terms of Use (posted 13-Oct-2026) forbid building a database, archive or tracking feed from the
    free website without a paid subscription, so the 23-Oct decision is paused and every FBIL path refuses."""
    assert "fbil_rates" not in data_rights.DECISIONS
    why = data_rights.PENDING["fbil_rates"]
    assert "13-Oct-2026" in why and "9.2" in why and "#476" in why
    with pytest.raises(data_rights.NoDecision, match="fbil_rates"):
        data_rights.require("fbil_rates")


def test_every_decision_is_dated_sourced_and_a_known_choice():
    for k, d in data_rights.DECISIONS.items():
        assert d.dataset == k and d.choice in {"accept-personal", "license", "drop"}
        assert date.fromisoformat(d.decided) and re.fullmatch(r"issue #\d+", d.ref) and d.terms_read


def test_the_module_states_the_safeguards_for_every_source():
    doc = data_rights.__doc__ or ""
    for s in ("FBIL", "IIMA", "MoSPI", "non-distributed", "commercial use", "403/429"):
        assert s in doc


@pytest.mark.parametrize("dataset", [nw.DATASET, iima.DATASET, mc.DATASET])
async def test_no_decision_no_request(monkeypatch, dataset):
    monkeypatch.delitem(data_rights.DECISIONS, dataset, raising=False)
    calls: list[str] = []
    http = counting_client(calls)
    with pytest.raises(data_rights.NoDecision, match=dataset):
        if dataset == nw.DATASET:

            async def quote(_s):  # fmt: skip
                calls.append("quote")
                return SimpleNamespace(ffmc=1, last_price=1)

            await nw.NiftyWeightsClient(http).nifty50(quote)
        elif dataset == iima.DATASET:
            await iima.IimaFactorsClient(http).latest_daily()
        else:
            await mc.MospiCpiClient(http).fetch(today=date(2026, 10, 23))
    await http.aclose()
    assert calls == []  # neither the list, the members' quotes, nor any page was asked for


@pytest.mark.parametrize("dataset", [nw.DATASET, iima.DATASET, mc.DATASET])
async def test_a_recorded_decision_lets_the_request_through(dataset):
    calls: list[str] = []
    http = counting_client(calls)
    with pytest.raises((nw.IndexWeightsError, iima.IimaError, mc.MospiError), match="HTTP 503"):
        if dataset == nw.DATASET:
            await nw.NiftyWeightsClient(http).nifty50(lambda _s: None)
        elif dataset == iima.DATASET:
            await iima.IimaFactorsClient(http).latest_daily()
        else:
            await mc.MospiCpiClient(http).fetch(today=date(2026, 10, 23))
    await http.aclose()
    assert len(calls) == 1  # the gate passed and the first request was made
