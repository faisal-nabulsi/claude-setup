"""Local web scraping MCP server built on Crawl4AI. No credits, no API keys.

Tools: scrape (web page or PDF -> Markdown), crawl (follow links on one site),
map_links (list a page's links), screenshot (PNG of a page).

Startup is cheap on purpose: crawl4ai is imported and a browser launched only
inside a tool call, and the browser closes after each call. That keeps the
initialize handshake well under Claude Code's 30 s limit even when the Mac swaps.
"""
import contextlib
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP, Image

os.environ.setdefault("CRAWL4_AI_BASE_DIRECTORY", str(Path.home() / ".crawl4ai"))
os.environ.setdefault("CRAWL4AI_NO_CLOUD_NOTICE", "1")
SHOT_DIR = Path.home() / "code" / "crawl4ai-mcp" / "screenshots"
DEFAULT_MAX_CHARS = 40_000

mcp = FastMCP("crawl4ai", log_level="WARNING")


def _quiet():
    """Crawl4AI logs to stdout; stdout carries the MCP protocol. Send logs to stderr."""
    return contextlib.redirect_stdout(sys.stderr)


def _browser_cfg(stealth: bool = True):
    from crawl4ai import BrowserConfig

    return BrowserConfig(headless=True, verbose=False, enable_stealth=stealth)


def _trim(text: str, max_chars: int) -> str:
    text = text or ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n\n[... cut at {max_chars:,} of {len(text):,} characters; raise max_chars to see more]"


async def _is_pdf(url: str) -> bool:
    if urlparse(url).path.lower().endswith(".pdf"):
        return True
    import httpx

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=10) as c:
            r = await c.head(url)
            return "application/pdf" in r.headers.get("content-type", "").lower()
    except Exception:
        return False


def _markdown(result, fit: bool) -> str:
    md = result.markdown
    if md is None:
        return ""
    if fit and getattr(md, "fit_markdown", None):
        return md.fit_markdown
    return getattr(md, "raw_markdown", None) or str(md)


def _header(result, extra: str = "") -> str:
    meta = result.metadata or {}
    title = meta.get("title") or ""
    return f"URL: {result.redirected_url or result.url}\nStatus: {result.status_code}\nTitle: {title}{extra}\n\n"


async def _scrape_pdf(url: str, max_chars: int) -> str:
    from crawl4ai import AsyncWebCrawler, CacheMode, CrawlerRunConfig
    from crawl4ai.processors.pdf import PDFContentScrapingStrategy, PDFCrawlerStrategy

    with _quiet():
        async with AsyncWebCrawler(crawler_strategy=PDFCrawlerStrategy()) as crawler:
            result = await crawler.arun(
                url=url,
                config=CrawlerRunConfig(
                    scraping_strategy=PDFContentScrapingStrategy(),
                    cache_mode=CacheMode.BYPASS,
                    verbose=False,
                ),
            )
    if not result.success:
        return f"PDF scrape failed for {url}: {result.error_message}"
    pages = (result.metadata or {}).get("pages") or ""
    body = _markdown(result, fit=False) or result.cleaned_html or ""
    return _header(result, f"\nType: PDF {pages}".rstrip()) + _trim(body, max_chars)


@mcp.tool()
async def scrape(
    url: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    wait_for: str | None = None,
    scroll: bool = False,
    main_content_only: bool = False,
    js_code: str | None = None,
) -> str:
    """Fetch one URL in a real headless Chromium and return clean Markdown.

    Handles JavaScript-heavy pages and PDFs (detected by .pdf or Content-Type).
    - wait_for: CSS selector ("css:.price") or JS condition ("js:() => ...") to wait for.
    - scroll: scroll the whole page first, for lazy-loaded / infinite-scroll content.
    - main_content_only: drop nav, footers and boilerplate (pruned "fit" Markdown).
    - js_code: JavaScript to run before capture (e.g. click a "load more" button).
    """
    started = time.time()
    if await _is_pdf(url):
        return await _scrape_pdf(url, max_chars)

    from crawl4ai import AsyncWebCrawler, CacheMode, CrawlerRunConfig
    from crawl4ai.content_filter_strategy import PruningContentFilterLXML
    from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

    md_gen = DefaultMarkdownGenerator(content_filter=PruningContentFilterLXML()) if main_content_only else None
    cfg = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        wait_for=wait_for,
        scan_full_page=scroll,
        js_code=js_code,
        remove_overlay_elements=True,
        remove_consent_popups=True,
        page_timeout=60_000,
        verbose=False,
        **({"markdown_generator": md_gen} if md_gen else {}),
    )
    with _quiet():
        async with AsyncWebCrawler(config=_browser_cfg()) as crawler:
            result = await crawler.arun(url=url, config=cfg)
    if not result.success:
        return f"Scrape failed for {url} (status {result.status_code}): {result.error_message}"
    took = f"\nTook: {time.time() - started:.1f}s"
    return _header(result, took) + _trim(_markdown(result, main_content_only), max_chars)


@mcp.tool()
async def crawl(
    url: str,
    max_pages: int = 10,
    max_depth: int = 1,
    max_chars_per_page: int = 6_000,
) -> str:
    """Start at url and follow same-site links breadth-first; return each page's Markdown.

    Keep max_pages small (default 10, hard cap 50): each page loads a real browser tab.
    """
    from crawl4ai import AsyncWebCrawler, BFSDeepCrawlStrategy, CacheMode, CrawlerRunConfig

    max_pages = max(1, min(max_pages, 50))
    cfg = CrawlerRunConfig(
        deep_crawl_strategy=BFSDeepCrawlStrategy(max_depth=max_depth, include_external=False, max_pages=max_pages),
        cache_mode=CacheMode.BYPASS,
        remove_overlay_elements=True,
        page_timeout=45_000,
        verbose=False,
    )
    with _quiet():
        async with AsyncWebCrawler(config=_browser_cfg()) as crawler:
            results = await crawler.arun(url=url, config=cfg)
    if not isinstance(results, list):
        results = [results]
    out = [f"Crawled {len(results)} page(s) from {url}\n"]
    for r in results:
        if r.success:
            out.append("---\n" + _header(r) + _trim(_markdown(r, False), max_chars_per_page))
        else:
            out.append(f"---\nURL: {r.url}\nFAILED: {r.error_message}")
    return "\n".join(out)


@mcp.tool()
async def map_links(url: str, include_external: bool = False, limit: int = 200) -> str:
    """Load a page and list its links (internal, plus external if asked) with their link text."""
    from crawl4ai import AsyncWebCrawler, CacheMode, CrawlerRunConfig

    with _quiet():
        async with AsyncWebCrawler(config=_browser_cfg()) as crawler:
            r = await crawler.arun(url=url, config=CrawlerRunConfig(cache_mode=CacheMode.BYPASS, verbose=False))
    if not r.success:
        return f"Failed for {url}: {r.error_message}"
    groups = ["internal"] + (["external"] if include_external else [])
    lines, seen = [], set()
    for g in groups:
        for link in (r.links or {}).get(g, []):
            href = link.get("href")
            if href and href not in seen:
                seen.add(href)
                lines.append(f"[{g}] {href}  {(link.get('text') or '').strip()[:80]}")
    return _header(r, f"\nLinks: {len(lines)}") + "\n".join(lines[:limit])


@mcp.tool()
async def screenshot(url: str, full_page: bool = False, settle_seconds: float = 3.0) -> list:
    """Take a PNG screenshot of a page (top of page, 1280x900 viewport unless full_page).

    Saves it under ~/code/crawl4ai-mcp/screenshots and returns it.
    settle_seconds: extra wait after load so client-side apps finish drawing.
    """
    import asyncio

    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 1280, "height": 900})
            try:
                await page.goto(url, wait_until="load", timeout=45_000)
            except Exception as e:  # slow ad-heavy pages: shoot what has drawn
                if "Timeout" not in type(e).__name__:
                    return [f"Screenshot failed for {url}: {e}"]
            await asyncio.sleep(settle_seconds)
            await page.evaluate("window.scrollTo(0, 0)")
            data = await page.screenshot(full_page=full_page, type="png")
        finally:
            await browser.close()
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    name = (urlparse(url).netloc or "page").replace(":", "_") + time.strftime("-%Y%m%d-%H%M%S.png")
    path = SHOT_DIR / name
    path.write_bytes(data)
    return [f"Saved {path}", Image(data=data, format="png")]


if __name__ == "__main__":
    mcp.run()
