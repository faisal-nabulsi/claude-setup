# crawl4ai-mcp

Local, credit-free scraping MCP server for Claude Code (built 2026-11-16 as the Firecrawl replacement).

- `server.py`: stdio MCP server, tools `scrape` (web page or PDF to Markdown), `crawl` (same-site BFS, max 50 pages),
  `map_links`, `screenshot` (saved to `screenshots/`).
- `.venv`: uv venv, Python 3.12, crawl4ai 0.9.5, mcp 1.30 (pinned `<2`: mcp 2.x renamed FastMCP), pypdf.
- Browser: Playwright headless shell `chromium_headless_shell-1255` in ~/Library/Caches/ms-playwright (206 MB).
- Registered with: `claude mcp add -s user crawl4ai -- ~/code/crawl4ai-mcp/.venv/bin/python ~/code/crawl4ai-mcp/server.py`
- Remove with: `claude mcp remove crawl4ai -s user`

Rebuild from scratch:
```bash
cd ~/code/crawl4ai-mcp
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python "crawl4ai==0.9.5" "mcp>=1.2,<2" pypdf
.venv/bin/python -m playwright install --only-shell chromium
```
If you upgrade crawl4ai/playwright, rerun the last line: each Playwright version wants its own Chromium build.
