from __future__ import annotations

import asyncio
import json
import sys

from tests.tools.conftest import register_all_web_providers


def test_shared_search_only_backend_falls_through_to_local_extract(monkeypatch):
    from tools import web_tools

    register_all_web_providers()
    monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"backend": "ddgs"})
    monkeypatch.setattr(web_tools, "_ddgs_package_importable", lambda: True)
    monkeypatch.setattr(web_tools, "_is_tool_gateway_ready", lambda: False)

    try:
        assert web_tools._get_extract_backend() in {"local_first", "local"}
    finally:
        from agent.web_search_registry import _reset_for_tests

        _reset_for_tests()


def test_explicit_local_extract_backend_is_available(monkeypatch):
    from tools import web_tools

    register_all_web_providers()
    monkeypatch.setattr(
        web_tools,
        "_load_web_config",
        lambda: {"backend": "ddgs", "extract_backend": "local_first"},
    )

    try:
        assert web_tools._get_extract_backend() == "local_first"
        assert web_tools.check_web_api_key() is True
    finally:
        from agent.web_search_registry import _reset_for_tests

        _reset_for_tests()


def test_local_first_uses_http_before_optional_crawlers(monkeypatch):
    from plugins.web.local_first import provider as local_provider

    async def run_test():
        local_provider.clear_local_first_cache_for_tests()
        calls: list[str] = []

        async def fake_http(url, *, timeout_secs, max_output_chars):
            calls.append("http")
            return local_provider._ExtractAttempt(
                provider="http",
                title="Example",
                content="Static page content that is long enough to be useful.",
                diagnostics=["http ok"],
            )

        async def fail_crawler(*args, **kwargs):
            calls.append("crawler")
            raise AssertionError("crawler should not run after HTTP success")

        monkeypatch.setattr(local_provider, "_extract_via_http", fake_http)
        monkeypatch.setattr(local_provider, "_extract_via_crawl4ai", fail_crawler)
        monkeypatch.setattr(local_provider, "_extract_via_scrapling", fail_crawler)

        result = await local_provider.LocalFirstWebExtractProvider().extract(["https://example.com"])

        assert calls == ["http"]
        assert result[0]["metadata"]["local_provider"] == "http"
        assert result[0]["content"].startswith("Static page content")

    asyncio.run(run_test())


def test_local_first_negative_caches_missing_local_tools(monkeypatch):
    from plugins.web.local_first import provider as local_provider

    async def run_test():
        local_provider.clear_local_first_cache_for_tests()
        module_checks: list[str] = []

        async def fail_http(*args, **kwargs):
            raise RuntimeError("http failed")

        def fake_module_missing(name: str) -> bool:
            module_checks.append(name)
            return True

        monkeypatch.setattr(local_provider, "_extract_via_http", fail_http)
        monkeypatch.setattr(local_provider, "_module_missing", fake_module_missing)
        monkeypatch.setattr(local_provider, "_failure_ttl_secs", lambda: 60)

        provider = local_provider.LocalFirstWebExtractProvider()
        first = await provider.extract(["https://example.com/app"])
        second = await provider.extract(["https://example.com/app"])

        assert first[0]["error"]
        assert second[0]["error"]
        assert module_checks == ["crawl4ai", "scrapling"]
        assert "cached local crawler failure" in second[0]["error"]

    asyncio.run(run_test())


def test_local_subprocess_timeout_kills_process():
    from plugins.web.local_first.provider import _run_bounded_subprocess

    async def run_test():
        script = "import time; time.sleep(2)"

        try:
            await _run_bounded_subprocess(
                "crawl4ai",
                [sys.executable, "-c", script],
                timeout_secs=1,
                max_output_chars=100_000,
            )
        except RuntimeError as exc:
            assert "timed out after 1s" in str(exc)
        else:
            raise AssertionError("subprocess timeout did not fire")

    asyncio.run(run_test())


def test_web_extract_with_shared_ddgs_uses_local_provider(monkeypatch):
    from plugins.web.local_first import provider as local_provider
    from tools import web_tools

    async def run_test():
        register_all_web_providers()
        local_provider.clear_local_first_cache_for_tests()
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"backend": "ddgs"})
        monkeypatch.setattr(web_tools, "_ddgs_package_importable", lambda: True)
        monkeypatch.setattr(web_tools, "_is_tool_gateway_ready", lambda: False)
        monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False, raising=False)

        async def allow_url(url: str) -> bool:
            return True

        async def fake_http(url, *, timeout_secs, max_output_chars):
            return local_provider._ExtractAttempt(
                provider="http",
                title="Example",
                content="Local-first extracted content from a static page.",
                diagnostics=["http ok"],
            )

        monkeypatch.setattr(web_tools, "async_is_safe_url", allow_url)
        monkeypatch.setattr(local_provider, "_extract_via_http", fake_http)

        try:
            raw = await web_tools.web_extract_tool(["https://example.com"])
            result = json.loads(raw)
            assert result["results"][0]["title"] == "Example"
            assert "Local-first extracted content" in result["results"][0]["content"]
        finally:
            from agent.web_search_registry import _reset_for_tests

            _reset_for_tests()

    asyncio.run(run_test())
