"""The pinned backtest engine for vault reads (#364, evals/experiments/vault/PREREG.md §2.7).

Pinned on 26-Oct-2026, in its own commit, before any engine-pinned read, once its prerequisites had merged: P0.2
(#352), P2.7(a)'s next-print fill variant (#382, PR #432), P1.3's dated lots and charges (#365, PR #440) and the
owner's pre-2020 charge decisions (#471, PR #472). The hash is vault.registry.engine_hash(): SHA-256 over
backtest/*.py, fincalc/options.py and fincalc/charges.py. From now on those files are frozen for vault studies: any
change makes vault.guard refuse every engine-pinned study (and tests/test_vault_guard.py fails), so a change there
needs a new registration, never a quiet re-pin.
"""

ENGINE_HASH: str | None = "9a4a24cfef56177ed465c1c9f2edcc73fcdcee99b5f85871753bcb0028885f9f"
