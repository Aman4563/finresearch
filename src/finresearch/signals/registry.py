"""Signal providers by asset class. Each asset lives in its own module (finresearch.signals.<asset>) and registers an
async provider `(instrument, ctx) -> Signal` with `@register("<asset>")`. Modules that don't exist yet are skipped."""

from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable
from typing import Any

from finresearch.signals.base import Signal

Provider = Callable[[str, dict[str, Any]], Awaitable[Signal]]

ASSETS = ("ipo", "stock", "fund", "bond", "fno")
_PROVIDERS: dict[str, Provider] = {}
_loaded = False


def register(asset: str) -> Callable[[Provider], Provider]:
    if asset not in ASSETS:
        raise ValueError(f"unknown asset {asset!r}; expected one of {ASSETS}")

    def deco(fn: Provider) -> Provider:
        _PROVIDERS[asset] = fn
        return fn

    return deco


def _load() -> None:
    global _loaded
    if _loaded:
        return
    for asset in ASSETS:
        try:
            importlib.import_module(f"finresearch.signals.{asset}")
        except ModuleNotFoundError as e:
            if e.name != f"finresearch.signals.{asset}":
                raise  # a real import error inside the module, not a missing module
    _loaded = True


def providers() -> dict[str, Provider]:
    _load()
    return dict(_PROVIDERS)


def get_provider(asset: str) -> Provider | None:
    return providers().get(asset)
