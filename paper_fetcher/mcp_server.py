"""MCP server exposing paper-fetcher tools for Claude Code."""

import logging
import sys

from mcp.server.fastmcp import FastMCP

from .config import Config
from .fetcher import PaperFetcher
from .sources import semantic_scholar

# Logging must go to stderr (stdout is used by MCP stdio transport)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)

mcp = FastMCP("paper-fetcher")

# Lazy-initialized shared fetcher instance
_fetcher: PaperFetcher | None = None


def _get_fetcher() -> PaperFetcher:
    global _fetcher
    if _fetcher is None:
        # Non-interactive: the MCP server can't open a login browser, so on an
        # expired session it reports back instead of hanging.
        _fetcher = PaperFetcher(Config.load(), interactive=False)
    return _fetcher


_LOGIN_HINT = (
    "\n\n---\n"
    "⚠️ **HKU EZproxy 登录已过期（或尚未登录）**，付费墙全文无法获取。\n"
    "请在终端运行一次：\n\n"
    "```\npaper-fetcher login\n```\n\n"
    "浏览器弹出后登录 HKU，cookie 会保存下来；登录后重新调用本工具即可。\n"
    "（cookie 有效期较短，过期后重跑上面这条命令即可。）"
)


@mcp.tool()
async def fetch_paper(identifier: str, format: str = "markdown") -> str:
    """Fetch an academic paper's full text by DOI or URL.

    Uses Open Access sources (Unpaywall, arXiv) first, then falls back
    to HKU EZproxy for paywalled content. Results are cached locally.

    Args:
        identifier: DOI (e.g. "10.1038/nphys1509") or article URL.
        format: Output format - "markdown" (default), "json", or "text".
    """
    fetcher = _get_fetcher()
    paper = fetcher.fetch(identifier)

    # If EZproxy login is needed (and we couldn't get full text), tell the user
    # exactly how to fix it instead of silently returning an abstract or nothing.
    login_needed = fetcher._auth is not None and fetcher._auth.session_expired

    if format == "json":
        body = paper.to_json()
    elif format == "text":
        body = paper.to_text()
    else:
        body = paper.to_markdown(include_pdf_path=True)

    if not paper.full_text:
        if login_needed:
            header = (
                f"Only metadata/abstract available for: {identifier}\n"
                f"Title: {paper.title}\nURL: {paper.url}"
                if paper.abstract
                else f"Could not retrieve full text for: {identifier}\n"
                f"Title: {paper.title}\nURL: {paper.url}"
            )
            return header + _LOGIN_HINT
        if not paper.abstract:
            return (
                f"Could not extract full text for: {identifier}\n"
                f"Title: {paper.title}\nURL: {paper.url}"
            )

    return body


@mcp.tool()
async def search_papers(query: str, limit: int = 10, year_range: str = "") -> str:
    """Search for academic papers via Semantic Scholar.

    Returns a list of papers with titles, authors, DOIs, and citation counts.
    Use the DOIs from results with fetch_paper to get full text.

    Args:
        query: Search query (e.g. "organic photovoltaics silver nanowire").
        limit: Maximum number of results (1-100, default 10).
        year_range: Optional year filter (e.g. "2020-2024" or "2020-").
    """
    results = semantic_scholar.search(
        query, limit=limit, year_range=year_range or None
    )

    if not results:
        return "No results found."

    lines = [f"Found {len(results)} results:\n"]
    for i, r in enumerate(results, 1):
        authors_str = ", ".join(r.authors[:3])
        if len(r.authors) > 3:
            authors_str += " et al."

        lines.append(f"### {i}. {r.title}")
        lines.append(f"- **Authors:** {authors_str}")
        if r.year:
            lines.append(f"- **Year:** {r.year}")
        if r.journal:
            lines.append(f"- **Journal:** {r.journal}")
        if r.doi:
            lines.append(f"- **DOI:** {r.doi}")
        elif r.arxiv_id:
            lines.append(f"- **arXiv:** {r.arxiv_id}")
        lines.append(f"- **Citations:** {r.citation_count}")
        if r.abstract:
            lines.append(f"- **Abstract:** {r.abstract[:200]}...")
        lines.append("")

    return "\n".join(lines)


@mcp.tool()
async def get_paper_metadata(doi: str) -> str:
    """Get metadata for a paper by DOI from Semantic Scholar.

    Returns title, authors, year, abstract, citation count, and identifiers.
    Lighter than fetch_paper - does not download full text.

    Args:
        doi: The DOI of the paper (e.g. "10.1038/nphys1509").
    """
    result = semantic_scholar.get_paper(f"DOI:{doi}")
    if result is None:
        return f"Paper not found for DOI: {doi}"

    lines = [f"# {result.title}"]
    if result.authors:
        lines.append(f"**Authors:** {', '.join(result.authors)}")
    if result.year:
        lines.append(f"**Year:** {result.year}")
    if result.journal:
        lines.append(f"**Journal:** {result.journal}")
    lines.append(f"**DOI:** {result.doi}")
    if result.arxiv_id:
        lines.append(f"**arXiv:** {result.arxiv_id}")
    lines.append(f"**Citations:** {result.citation_count}")
    if result.abstract:
        lines.append(f"\n## Abstract\n\n{result.abstract}")

    return "\n".join(lines)


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
