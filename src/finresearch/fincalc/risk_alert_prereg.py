"""The pinned hashes of fincalc/risk_alert_specs.py (#520): the whole record and each touch variant, committed and
pushed before any code that replays stored marks existed. `require_registered` refuses the replay while
specs.registration_hash() differs from HASH."""

from finresearch.fincalc import risk_alert_specs as specs

HASH = "c0ae2e722f941156cf3e3f7377d0484d708497332d900126c9c4dca4a5df7f16"
VARIANT_HASHES = {
    "touch-reflection-v1": "fa8af077ae8554be57d4b63bc1255aec738949576a7337c39e44c2b5280adb40",
    "touch-exact-v2": "0efb05734c344497c63d8b8443667e48b5acb43856fc351242dfb08bdcd96997",
}
REGISTERED_ON = "2026-11-23"
NOTE = ("Post hoc: variant 1 shipped on 22-Oct-2026 (PR #453) without a hash and is pinned here from the unedited "
        "code; variant 2 was chosen after the oracle suite measured variant 1 on synthetic inputs. Registered before "
        "any replay code existed: no fno_mark row was read for it, in the live database or any copy.")  # fmt: skip


class PreregistrationError(RuntimeError):
    pass


def require_registered() -> str:
    """The registered hash; PreregistrationError when the record, a label or the level it reads has changed."""
    h = specs.registration_hash()
    if h != HASH:
        raise PreregistrationError(
            f"fincalc/risk_alert_specs.py hashes to {h[:12]}… but the registered hash is {HASH[:12]}…: the touch "
            "replay is refused. Record the change in evals/experiments/risk_alerts/ADDENDUM.md as a new variant "
            "and SPEC_VERSION and commit its hash first."
        )
    return h
