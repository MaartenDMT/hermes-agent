"""Local-first web extraction plugin."""

from __future__ import annotations

from plugins.web.local_first.provider import LocalFirstWebExtractProvider


def register(ctx) -> None:
    """Register local-first extract provider aliases."""
    ctx.register_web_search_provider(LocalFirstWebExtractProvider("local_first"))
    ctx.register_web_search_provider(LocalFirstWebExtractProvider("local"))
