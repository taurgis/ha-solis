"""Mock SolisCloud API used by tests and the Home Assistant lab."""

from __future__ import annotations

__all__ = ["create_app"]


def create_app(*args: object, **kwargs: object) -> object:
    """Import the app lazily so ``python -m mock_soliscloud`` can set the path."""
    from .app import create_app as _create_app

    return _create_app(*args, **kwargs)
