"""The pinned backtest engine for vault reads (#364, evals/experiments/vault/PREREG.md §2.7).

None until P0.2 (#352), P2.7(a)'s next-print fill variant (#382) and P1.3's dated lots and charges (#365) are merged.
Then set ENGINE_HASH = vault.registry.engine_hash() in its own commit, pushed before the first engine-pinned read.
While it is None, vault.guard refuses every engine-pinned study.
"""

ENGINE_HASH: str | None = None
