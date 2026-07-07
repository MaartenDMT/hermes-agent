"""Local-first web extraction provider.

This provider is intentionally extract-only.
It gives workhorse agents a free default path for ``web_extract`` when search
uses a search-only backend such as ddgs, Brave, SearXNG, or xAI.
"""

from __future__ import annotations

import asyncio
import html
import importlib.util
import json
import logging
import re
import sys
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import httpx

from agent.web_search_provider import WebSearchProvider
from tools.url_safety import async_is_safe_url
from tools.website_policy import check_website_access

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECS = 30
_DEFAULT_MAX_OUTPUT_CHARS = 1_000_000
_DEFAULT_FAILURE_TTL_SECS = 300
_MIN_USEFUL_TEXT_CHARS = 80
_TEXT_LIKE_TYPES = (
    "text/html",
    "text/plain",
    "text/markdown",
    "application/xhtml+xml",
    "application/xml",
    "text/xml",
    "application/json",
)


@dataclass(frozen=True)
class _ExtractAttempt:
    provider: str
    title: str
    content: str
    diagnostics: List[str]


@dataclass(frozen=True)
class _Failure:
    provider: str
    reason: str


_failure_cache: dict[tuple[str, str], tuple[float, _Failure]] = {}


def clear_local_first_cache_for_tests() -> None:
    """Clear provider caches.

    Test-only helper, but harmless for callers that need to flush diagnostics.
    """
    _failure_cache.clear()


def _load_web_config() -> dict:
    try:
        from hermes_cli.config import load_config

        cfg = load_config().get("web", {})
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _local_extract_config() -> dict:
    cfg = _load_web_config().get("local_extract", {})
    return cfg if isinstance(cfg, dict) else {}


def _positive_int(value: Any, default: int, *, minimum: int = 1, maximum: int = 86_400) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(parsed, maximum))


def _timeout_secs() -> int:
    cfg = _local_extract_config()
    return _positive_int(cfg.get("timeout_seconds"), _DEFAULT_TIMEOUT_SECS, minimum=1, maximum=300)


def _max_output_chars() -> int:
    cfg = _local_extract_config()
    return _positive_int(
        cfg.get("max_output_chars"),
        _DEFAULT_MAX_OUTPUT_CHARS,
        minimum=10_000,
        maximum=5_000_000,
    )


def _failure_ttl_secs() -> int:
    cfg = _local_extract_config()
    return _positive_int(
        cfg.get("failure_cache_ttl_seconds"),
        _DEFAULT_FAILURE_TTL_SECS,
        minimum=0,
        maximum=3600,
    )


def _cache_failure(provider: str, url: str, reason: str) -> None:
    ttl = _failure_ttl_secs()
    if ttl <= 0:
        return
    _failure_cache[(provider, url)] = (
        time.monotonic() + ttl,
        _Failure(provider=provider, reason=reason),
    )


def _cached_failure(provider: str, url: str) -> Optional[_Failure]:
    key = (provider, url)
    entry = _failure_cache.get(key)
    if not entry:
        return None
    expires_at, failure = entry
    if expires_at <= time.monotonic():
        _failure_cache.pop(key, None)
        return None
    return _Failure(
        provider=failure.provider,
        reason=f"cached local crawler failure: {failure.reason}",
    )


def _module_missing(module_name: str) -> bool:
    return importlib.util.find_spec(module_name) is None


def _normalize_whitespace(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


class _ReadableHTMLParser(HTMLParser):
    """Small stdlib readability fallback.

    It is not a full Readability clone.
    It strips non-content tags and preserves enough headings, paragraphs, and
    list structure to make static pages and docs useful.
    """

    _SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "canvas"}
    _BLOCK_TAGS = {
        "article",
        "main",
        "section",
        "header",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "p",
        "div",
        "br",
        "li",
        "pre",
        "blockquote",
        "tr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.body_parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self._link_href: Optional[str] = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
            return
        if self._skip_depth:
            return
        if tag in self._BLOCK_TAGS:
            self.body_parts.append("\n")
        if tag == "li":
            self.body_parts.append("- ")
        if tag == "a":
            attrs_dict = dict(attrs)
            href = attrs_dict.get("href")
            self._link_href = href.strip() if href else None

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag == "title":
            self._in_title = False
            return
        if self._skip_depth:
            return
        if tag == "a":
            self._link_href = None
        if tag in self._BLOCK_TAGS:
            self.body_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = html.unescape(data).strip()
        if not text:
            return
        if self._in_title:
            self.title_parts.append(text)
            return
        if self._link_href and self._link_href.startswith(("http://", "https://")):
            self.body_parts.append(f"{text} ({self._link_href}) ")
        else:
            self.body_parts.append(text + " ")

    @property
    def title(self) -> str:
        return _normalize_whitespace(" ".join(self.title_parts))

    @property
    def text(self) -> str:
        return _normalize_whitespace("".join(self.body_parts))


def _html_to_text(raw: str) -> tuple[str, str]:
    parser = _ReadableHTMLParser()
    parser.feed(raw)
    return parser.title, parser.text


def _normalize_crawler_payload(provider: str, url: str, stdout: str) -> _ExtractAttempt:
    text = stdout.strip()
    title = ""
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            title = str(parsed.get("title") or parsed.get("data", {}).get("title") or "")
            text = str(
                parsed.get("markdown")
                or parsed.get("content")
                or parsed.get("text")
                or parsed.get("data", {}).get("markdown")
                or parsed.get("data", {}).get("content")
                or text
            )
    except Exception:
        pass
    text = _normalize_whitespace(text)
    if not text:
        raise RuntimeError(f"{provider} returned empty content")
    if len(text) > _max_output_chars():
        text = text[: _max_output_chars()] + "\n\n[local extractor output truncated at max_output_chars]"
    return _ExtractAttempt(
        provider=provider,
        title=title,
        content=text,
        diagnostics=[f"{provider} extracted {url}"],
    )


async def _run_bounded_subprocess(
    provider: str,
    args: list[str],
    *,
    timeout_secs: int,
    max_output_chars: int,
) -> str:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def _read_limited(stream: asyncio.StreamReader, label: str) -> str:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_output_chars:
                process.kill()
                raise RuntimeError(f"{provider} {label} exceeded {max_output_chars} bytes")
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", errors="replace")

    try:
        stdout_task = asyncio.create_task(_read_limited(process.stdout, "stdout"))
        stderr_task = asyncio.create_task(_read_limited(process.stderr, "stderr"))
        await asyncio.wait_for(process.wait(), timeout=timeout_secs)
        stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise RuntimeError(f"{provider} timed out after {timeout_secs}s") from exc
    except Exception:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise

    if process.returncode != 0:
        stderr_excerpt = stderr.strip()[:1000]
        raise RuntimeError(
            f"{provider} exited with code {process.returncode}"
            + (f": {stderr_excerpt}" if stderr_excerpt else "")
        )
    if not stdout.strip():
        raise RuntimeError(f"{provider} returned no output")
    return stdout


async def _extract_via_http(url: str, *, timeout_secs: int, max_output_chars: int) -> _ExtractAttempt:
    policy = check_website_access(url)
    if policy is not None:
        raise RuntimeError(policy.get("message") or policy.get("rule") or "blocked by website policy")

    current_url = url
    headers = {"User-Agent": "Hermes-Agent local web_extract"}
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_secs),
        headers=headers,
        follow_redirects=False,
    ) as client:
        for _ in range(5):
            response = await client.get(current_url)
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    break
                next_url = urljoin(current_url, location)
                if not await async_is_safe_url(next_url):
                    raise RuntimeError("redirect blocked: target is private or internal")
                current_url = next_url
                continue
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
            if content_type and not any(content_type.startswith(t) for t in _TEXT_LIKE_TYPES):
                raise RuntimeError(f"unsupported content type: {content_type}")
            raw = response.text
            if len(raw) > max_output_chars:
                raw = raw[:max_output_chars]
            if "html" in content_type or "<html" in raw[:500].lower():
                title, text = _html_to_text(raw)
            else:
                title, text = "", _normalize_whitespace(raw)
            if len(text) < _MIN_USEFUL_TEXT_CHARS:
                raise RuntimeError("plain HTTP returned too little readable text")
            return _ExtractAttempt(
                provider="http",
                title=title,
                content=text,
                diagnostics=[f"plain HTTP/readability fetched {current_url}"],
            )
    raise RuntimeError("too many redirects")


async def _extract_via_crawl4ai(url: str, *, timeout_secs: int, max_output_chars: int) -> _ExtractAttempt:
    if _module_missing("crawl4ai"):
        raise RuntimeError("crawl4ai package is not installed")
    script = (
        "import asyncio, json, sys\n"
        "from crawl4ai import AsyncWebCrawler\n"
        "async def main():\n"
        "    async with AsyncWebCrawler() as crawler:\n"
        "        result = await crawler.arun(url=sys.argv[1])\n"
        "        print(json.dumps({\n"
        "            'title': getattr(result, 'title', '') or '',\n"
        "            'markdown': getattr(result, 'markdown', '') or getattr(result, 'cleaned_html', '') or ''\n"
        "        }))\n"
        "asyncio.run(main())\n"
    )
    stdout = await _run_bounded_subprocess(
        "crawl4ai",
        [sys.executable, "-c", script, url],
        timeout_secs=timeout_secs,
        max_output_chars=max_output_chars,
    )
    return _normalize_crawler_payload("crawl4ai", url, stdout)


async def _extract_via_scrapling(url: str, *, timeout_secs: int, max_output_chars: int) -> _ExtractAttempt:
    if _module_missing("scrapling"):
        raise RuntimeError("scrapling package is not installed")
    script = (
        "import json, sys\n"
        "from scrapling.fetchers import StealthyFetcher\n"
        "page = StealthyFetcher.fetch(sys.argv[1])\n"
        "title = page.css_first('title::text')\n"
        "text = page.get_all_text(separator='\\n')\n"
        "print(json.dumps({'title': str(title) if title else '', 'markdown': text}))\n"
    )
    stdout = await _run_bounded_subprocess(
        "scrapling",
        [sys.executable, "-c", script, url],
        timeout_secs=timeout_secs,
        max_output_chars=max_output_chars,
    )
    return _normalize_crawler_payload("scrapling", url, stdout)


class LocalFirstWebExtractProvider(WebSearchProvider):
    """Free, local-first extraction provider."""

    def __init__(self, name: str = "local_first") -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def display_name(self) -> str:
        return "Local-first extract" if self._name == "local_first" else "Local extract"

    def is_available(self) -> bool:
        return True

    def supports_search(self) -> bool:
        return False

    def supports_extract(self) -> bool:
        return True

    async def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        timeout_secs = _timeout_secs()
        max_output_chars = _max_output_chars()
        results: list[dict[str, Any]] = []

        for url in urls:
            failures: list[_Failure] = []
            attempt: Optional[_ExtractAttempt] = None
            for provider, extractor in (
                ("http", _extract_via_http),
                ("crawl4ai", _extract_via_crawl4ai),
                ("scrapling", _extract_via_scrapling),
            ):
                cached = _cached_failure(provider, url)
                if cached:
                    failures.append(cached)
                    continue
                try:
                    attempt = await extractor(
                        url,
                        timeout_secs=timeout_secs,
                        max_output_chars=max_output_chars,
                    )
                    break
                except Exception as exc:  # noqa: BLE001
                    reason = str(exc) or type(exc).__name__
                    failures.append(_Failure(provider=provider, reason=reason))
                    if provider in {"crawl4ai", "scrapling"}:
                        _cache_failure(provider, url, reason)
                    logger.debug("local extract provider %s failed for %s: %s", provider, url, reason)

            if attempt is None:
                diagnostics = "; ".join(f"{f.provider}: {f.reason}" for f in failures)
                results.append(
                    {
                        "url": url,
                        "title": "",
                        "content": "",
                        "raw_content": "",
                        "metadata": {"provider": self.name, "diagnostics": diagnostics},
                        "error": (
                            "Local-first extraction failed. "
                            f"Diagnostics: {diagnostics}. "
                            "Install crawl4ai or scrapling for JS-heavy pages, or explicitly configure "
                            "a hosted extract backend with a budget."
                        ),
                    }
                )
                continue

            diagnostics = attempt.diagnostics + [
                f"{f.provider} failed: {f.reason}" for f in failures
            ]
            results.append(
                {
                    "url": url,
                    "title": attempt.title,
                    "content": attempt.content,
                    "raw_content": attempt.content,
                    "metadata": {
                        "provider": self.name,
                        "local_provider": attempt.provider,
                        "diagnostics": diagnostics,
                    },
                }
            )

        return results

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Local-first extract",
            "badge": "free, local, extract only",
            "tag": (
                "Extracts with bounded plain HTTP/readability first, then optional "
                "local Crawl4AI and Scrapling. No hosted API is used."
            ),
            "env_vars": [],
        }
