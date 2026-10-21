"""The vault pack's recorded hashes (#364): committed after the pack's files (commit f9d9584, pushed 22-Oct-2026 before
any 2008-2021 file was loaded). vault.guard recomputes them from disk (vault.registry) and refuses any read on a
difference. Never edit these values: a changed or added study is a new pack, and once the vault has been read the
guard refuses every read under another pack hash (PREREG.md §2.3)."""

from finresearch.vault.registry import (
    engine_hash,  # noqa: F401  (PREREG.md §2.7 names it vault.pack.engine_hash)
)

REGISTERED_ON = "2026-10-22"
PACK_COMMIT = "f9d9584"  # the commit that registered the pack's files

PACK_HASH = "6facc33224b4c0f79a6615f4e61f7688f2e8d192cf598613bad0e8dcc2104dcf"
STUDY_HASHES: dict[str, str] = {
    "vault_data_audit": "13c26bc5f2f3602d37333c9eff4cac04dbf82e7d8ee1617267a718a58032f2ea",
    "library_v1_same_print": "abdfba1cd0eae31351bf6c6c6c24d5e7d843257292b589676521b6652437f41d",
    "library_v1_next_print": "82419602fd79f88dd3fa60883431b2bfca42bf4d403737960c67e77a91b79ab9",
    "horse_race_calibration": "e85f7278e183462d494d66b05b90d00114917697aa47beb90bbc60d9a2c63c62",
    "vrp_primary": "4f46711f4f9cd5d5962ca1c548e061f6b324027f27d6602a888eeedf7cb896a2",
    "vol_forecast_race": "eea0027c0aee26833bf3418a2fdbc893d00210faa4379dca0f5d87307c44c051",
    "vrp_seller_variant": "b435ee01a603fe21f6470fd74dd47d0aaecf607ac8b31e3f9a4e38b50b7c0dcb",
    "event_vol_fairness": "b28230d6a383b6bb6cf00ea140ac73ed68d5044a722ce212e7f970a9ae99d954",
    "event_skip_variant": "8a884267009cab611ed7197fa17078ef3828eb5ce839f0cae12a8f868ec3f567",
    "risk_budget_counterfactual": "63d1b794dcd73be6a47afff4f474b5f5d17cd43a4b0ef99629e56cc432bccacd",
    "crisis_day_replays": "865b4802821fe5f70002bf7d7424f8d1e652f0e815c203ba85215c0d6cb78a20",
    "stress_start_day_scan": "d0b387d36fab5742d938032de7f7363c6a8f47fbecf33f6357cc95639ce3d78c",
    "risk_model_samples": "8cf14326a835c88fe2084fae1adf3f2284f5c630f47646651bb1229b6c485761",
}
