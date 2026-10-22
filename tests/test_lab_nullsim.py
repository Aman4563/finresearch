"""#380: the pre-registered null experiment's code (evals/experiments/lab_rigour/PREREG.md §5), on tiny settings: the
full registered run is `finresearch fno lab null-sim` and its output is committed beside the PREREG."""

from __future__ import annotations

import math

from finresearch.stratlab import nullsim
from finresearch.stratlab.rigour import Family, NullSim

TINY = NullSim(noise_panels=4, noise_n=12, noise_t=320, planted_panels=3, planted_noise_n=8, planted_t=(320,),
               planted_annual_sr=40.0, incubation_runs=50, incubation_days=400)  # fmt: skip
FAM = Family(bootstrap_draws=200)


def test_planted_panels_use_the_daily_sharpe_and_one_planted_column():
    m = nullsim.planted_panel(nullsim.rng(1, "c", 0), 8, 5000, 1.5 / math.sqrt(252))
    assert m.shape == (5000, 9)
    assert (
        abs(m[:, 0].mean() / m[:, 0].std(ddof=1) - 1.5 / math.sqrt(252)) < 0.03
    )  # se of a SR over 5,000 days: 0.014
    assert abs(m[:, 1:].mean()) < 0.01


def test_the_tiny_run_reports_every_item_and_is_reproducible():
    a = nullsim.run(TINY, FAM)
    assert set(a["items"]) == {"A_noise_pbo", "B_noise_spa", "C_planted_t320", "D_incubation_power"}
    assert a["registration_hash"] and a["settings"]["null_sim"]["noise_panels"] == 4
    c = a["items"]["C_planted_t320"]
    assert c["mean_pbo"] < 0.2 and c["spa_reject_rate"] == 1.0  # an absurd SR of 40: always found
    assert a["items"]["D_incubation_power"]["power"] == 1.0
    assert 0.0 <= a["items"]["A_noise_pbo"]["mean_pbo"] <= 1.0
    assert nullsim.run(TINY, FAM)["items"] == a["items"]


def test_cli_null_sim_writes_the_json(tmp_path, monkeypatch):
    import json

    from typer.testing import CliRunner

    from finresearch import cli
    from finresearch.stratlab import rigour

    monkeypatch.setattr(rigour, "NULL_SIM", TINY)
    monkeypatch.setattr(rigour, "FAMILY", FAM)
    out = tmp_path / "sim.json"
    r = CliRunner().invoke(cli.app, ["fno", "lab", "null-sim", "--json", str(out)])
    assert r.exit_code == 0, r.output
    got = json.loads(out.read_text())
    assert got["settings"]["null_sim"]["noise_panels"] == 4 and "A_noise_pbo" in got["items"]
    assert "NOT the registered settings" in r.output and got["registered_settings"] is False
