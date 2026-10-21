"""The vault-read guard (#364): registered studies only, hashes from the committed pack, one logged read, fail closed.

The "vault" rows here are a handful of FABRICATED fno_bhav rows (symbol FAKEIDX, made-up prices) dated inside
2008-2021; no real file of that period exists in this project. The read log and the fake vault share the test DB.
"""

from __future__ import annotations

import shutil
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import delete, insert, select

from finresearch.config import REPO_ROOT
from finresearch.vault import engine_pin, guard, pack, registry
from finresearch.vault.registry import VaultSealed

H = pack.STUDY_HASHES


def test_the_committed_pack_matches_its_files():
    assert registry.pack_hash(REPO_ROOT) == pack.PACK_HASH
    assert set(H) == set(registry.STUDIES)
    for sid, st in registry.STUDIES.items():
        assert registry.study_hash(st, REPO_ROOT) == H[sid]
        assert all(r in registry.STUDIES for r in st.requires)
    assert engine_pin.ENGINE_HASH is None  # pinned only after P2.7(a) and P1.3 merge (PREREG.md §2.7)


def test_unregistered_or_wrong_hash_is_refused():
    with pytest.raises(VaultSealed, match="not a registered"):
        guard.verify("strategy_lab_iv_tercile", H["vrp_primary"])
    with pytest.raises(VaultSealed, match="presented hash"):
        guard.verify("vrp_primary", H["vol_forecast_race"])
    assert guard.verify("vrp_primary", H["vrp_primary"]).id == "vrp_primary"


def _copy_pack(tmp_path):
    for st in registry.STUDIES.values():
        for f in (*st.files, registry.RULES_FILE):
            (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPO_ROOT / f, tmp_path / f)
    return tmp_path


def test_a_changed_or_missing_registration_is_refused(tmp_path):
    root = _copy_pack(tmp_path)
    assert guard.verify("vrp_primary", H["vrp_primary"], root).id == "vrp_primary"  # an exact copy passes
    vrp = root / "evals/experiments/vault/vrp.md"
    vrp.write_text(vrp.read_text().replace("Newey-West t (lag 1)", "Newey-West t (lag 4)"))
    with pytest.raises(VaultSealed, match="changed or is missing"):
        guard.verify("vrp_primary", H["vrp_primary"], root)
    # another study's file changed: this study is intact but the pack is not
    with pytest.raises(VaultSealed, match="pack changed"):
        guard.verify("vault_data_audit", H["vault_data_audit"], root)
    (root / registry.RULES_FILE).unlink()
    with pytest.raises(VaultSealed, match="missing file"):
        guard.verify("vault_data_audit", H["vault_data_audit"], root)


@pytest.fixture
def vault(env, db_url):
    """The test DB as both the read log and a fake vault holding two fabricated 2008 rows."""
    from finresearch.db import session_scope
    from finresearch.db.models import FnoBhav, VaultRead

    def clean():
        with session_scope() as s:
            s.execute(delete(VaultRead))
            s.execute(delete(FnoBhav).where(FnoBhav.symbol == "FAKEIDX"))

    clean()
    with session_scope() as s:
        s.execute(insert(FnoBhav), [
            {"day": date(2008, 10, d), "symbol": "FAKEIDX", "instrument": "IDF", "expiry": date(2008, 10, 30),
             "strike": Decimal(0), "option_type": "XX", "settle": Decimal("1111.10"), "contracts": 7}
            for d in (23, 24)])  # fmt: skip
    yield db_url
    clean()


def _open(*a, **kw):
    with guard.vault_read(*a, **kw):
        pass


def _log(db_url):
    from finresearch.db import session_scope
    from finresearch.db.models import VaultRead

    with session_scope(db_url) as s:
        return [
            (r.study_id, r.read_no, r.study_hash, r.pack_hash)
            for r in s.scalars(select(VaultRead).order_by(VaultRead.id))
        ]


def test_one_registered_read_is_allowed_logged_and_never_repeated(vault):
    with pytest.raises(VaultSealed, match="only after vault_data_audit"):
        _open("vrp_primary", H["vrp_primary"], purpose="t", vault_url=vault)
    assert _log(vault) == []  # a refused read is not logged
    for _ in range(2):  # the aggregate audit alone is repeatable, numbered
        with guard.vault_read("vault_data_audit", H["vault_data_audit"], purpose="audit", vault_url=vault):
            pass
    with guard.vault_read("vrp_primary", H["vrp_primary"], purpose="P3.6 primary", vault_url=vault) as g:
        assert (g.study_id, g.read_no, g.open) == ("vrp_primary", 1, True)
        assert _log(vault)[-1] == (
            "vrp_primary",
            1,
            H["vrp_primary"],
            pack.PACK_HASH,
        )  # logged before the read
    assert g.open is False
    with pytest.raises(VaultSealed, match="already read the vault"):
        _open("vrp_primary", H["vrp_primary"], purpose="again", vault_url=vault)
    assert [(sid, n) for sid, n, *_ in _log(vault)] == [
        ("vault_data_audit", 1),
        ("vault_data_audit", 2),
        ("vrp_primary", 1),
    ]
    assert guard.reads_count() == 1  # the audit is not a hypothesis read
    row = guard.reads()[-1]
    assert row["engine_hash"] == registry.engine_hash() and row["purpose"] == "P3.6 primary"


def test_a_read_that_crashes_is_still_spent(vault):
    with guard.vault_read("vault_data_audit", H["vault_data_audit"], purpose="audit", vault_url=vault):
        pass
    with (
        pytest.raises(RuntimeError),
        guard.vault_read("event_vol_fairness", H["event_vol_fairness"], purpose="t", vault_url=vault),
    ):
        raise RuntimeError("study failed midway")
    with pytest.raises(VaultSealed, match="already read"):
        _open("event_vol_fairness", H["event_vol_fairness"], purpose="t", vault_url=vault)


def test_engine_pinned_studies_need_the_committed_pin(vault, monkeypatch):
    with guard.vault_read("vault_data_audit", H["vault_data_audit"], purpose="audit", vault_url=vault):
        pass
    sid = "library_v1_same_print"
    with pytest.raises(VaultSealed, match="no pin is committed"):
        _open(sid, H[sid], purpose="t", vault_url=vault)
    monkeypatch.setattr(engine_pin, "ENGINE_HASH", "0" * 64)
    with pytest.raises(VaultSealed, match="the engine hashes to"):
        _open(sid, H[sid], purpose="t", vault_url=vault)
    assert [r[0] for r in _log(vault)] == ["vault_data_audit"]
    monkeypatch.setattr(engine_pin, "ENGINE_HASH", registry.engine_hash())
    with guard.vault_read(sid, H[sid], purpose="library v1", vault_url=vault):
        pass
    assert [r[0] for r in _log(vault)] == ["vault_data_audit", sid]


def test_a_read_under_another_pack_seals_every_later_read(vault):
    from finresearch.db import session_scope
    from finresearch.db.models import VaultRead

    with session_scope() as s:
        s.add(VaultRead(study_id="vault_data_audit", study_hash=H["vault_data_audit"], read_no=1, pack_hash="f" * 64,
                        engine_hash="e" * 64, counts_in_n=False, purpose="an older pack"))  # fmt: skip
    with pytest.raises(VaultSealed, match="another pack"):
        _open("vault_data_audit", H["vault_data_audit"], purpose="t", vault_url=vault)


def test_no_vault_or_no_log_fails_closed_without_spending_the_read(vault, monkeypatch):
    with pytest.raises(VaultSealed, match="no vault database"):
        _open("vault_data_audit", H["vault_data_audit"], purpose="t")
    bad = vault.rsplit("/", 1)[0] + "/finresearch_p12_missing_test"
    with pytest.raises(VaultSealed, match="read log could not be"):
        _open("vault_data_audit", H["vault_data_audit"], purpose="t", vault_url=vault, log_url=bad)
    assert _log(vault) == []


def test_history_refuses_vault_rows_without_a_live_grant(vault):
    from finresearch.backtest.dbhistory import DbHistory

    with pytest.raises(VaultSealed, match="sealed 2008-2021 vault"):
        DbHistory(vault).sessions("FAKEIDX", date(2008, 1, 1), date(2008, 12, 31))
    with guard.vault_read("vault_data_audit", H["vault_data_audit"], purpose="audit", vault_url=vault) as g:
        with pytest.raises(VaultSealed):  # a grant for another URL does not open this one
            DbHistory(vault + "?application_name=x", vault_grant=g).sessions(
                "FAKEIDX", date(2008, 1, 1), date(2008, 12, 31)
            )
        h = DbHistory(vault, vault_grant=g)
        assert h.sessions("FAKEIDX", date(2008, 1, 1), date(2008, 12, 31)) == [
            date(2008, 10, 23),
            date(2008, 10, 24),
        ]
        assert h.day("FAKEIDX", date(2008, 10, 24)).futures == {date(2008, 10, 30): 1111.1}
    with pytest.raises(VaultSealed):  # the grant closed with its block
        h.sessions("FAKEIDX", date(2008, 1, 1), date(2008, 12, 31))


def test_history_without_vault_rows_needs_no_grant(env):
    from finresearch.backtest.dbhistory import DbHistory

    # the registered backtest asks for IV warm-up ranges before 2022: harmless while no vault row is stored
    assert DbHistory().iv_history("NIFTY", date(2020, 11, 1), date(2022, 1, 31)) == []
    assert DbHistory().sessions("NIFTY", date(2008, 1, 1), date(2021, 12, 31)) == []


async def test_the_fno_backfill_refuses_vault_days():
    from finresearch.fnohist import store

    with pytest.raises(VaultSealed, match="inside the sealed"):
        await store.backfill(date(2021, 12, 31), date(2022, 1, 5), stocks=[], http=object())
