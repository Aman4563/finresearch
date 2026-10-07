"""FinResearch: personal, fact-checked investment research (docs/ARCHITECTURE.md)."""

from importlib.metadata import PackageNotFoundError, version


def _version() -> str:
    """The installed package's version (pyproject.toml is the one source). "0+unknown" when the package metadata is
    missing (e.g. a bare source checkout without `uv sync`): an unknown version is never shown as a real number."""
    try:
        return version("finresearch")
    except PackageNotFoundError:
        return "0+unknown"


__version__ = _version()
