"""Local HTTP API for the research app (bound to 127.0.0.1 only)."""

from finresearch.api.app import create_app

__all__ = ["create_app"]
