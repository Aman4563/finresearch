"""Read-only broker connections: pull holdings, positions and trades from the user's own broker accounts.

See `base` (the contract and the read-only HTTP layer), `merge` (how broker data joins the portfolio without double
counting or overwriting), `sync` (one sync and the monitor's daily schedule), `store` (local, masked credentials),
`inbox` (CAS/tradebook/holdings files dropped into a folder), and docs/BROKER_SETUP.md for the per-broker setup.
Research and sources: the PR's brokers-research notes (official API docs, SEBI's 2025 retail API framework).
"""

from __future__ import annotations

from importlib import import_module

# key -> "module:Class" (imported lazily so the API starts without touching every connector)
_REGISTRY = {
    "groww": "groww:GrowwConnector",
    "zerodha": "zerodha:ZerodhaConnector",
    "upstox": "upstox:UpstoxConnector",
    "dhan": "dhan:DhanConnector",
}
# Not implemented: Angel One SmartAPI and Fyers (their docs could not be read to verify the response fields: see
# docs/BROKER_SETUP.md), and the RBI Account Aggregator (data flows only to regulated FIUs, not individuals).
INBOX_KEY = "cas_inbox"


class _Lazy(dict):
    def __getitem__(self, key: str):
        return connector_class(key)


CONNECTORS = _Lazy.fromkeys(_REGISTRY)


def connector_class(key: str):
    if key not in _REGISTRY:
        raise LookupError(f"unknown connection {key}")
    mod, cls = _REGISTRY[key].split(":")
    return getattr(import_module(f"finresearch.portfolio.connectors.{mod}"), cls)


def all_classes() -> list:
    return [connector_class(k) for k in _REGISTRY]
