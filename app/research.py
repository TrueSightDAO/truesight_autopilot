"""CrewAI research runner for autonomous marketing/infrastructure research.

Uses CrewAI under the hood to run multi-step autonomous research.
Progress is reported via a callback that the Telegram adapter hooks into.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

logger = logging.getLogger("autopilot.research")


def run_research(
    role: str,
    topic: str,
    target_repo: str,
    on_progress: Callable[[str], None] | None = None,
) -> str:
    """Run autonomous research using CrewAI.

    Args:
        role: role key from roles.py (e.g. 'content_marketing')
        topic: what to research (e.g. 'ceremonial cacao consumer demographics')
        target_repo: GitHub repo to file the report in (e.g. 'go_to_market')
        on_progress: called with status messages during research

    Returns:
        The final research report as a markdown string.
    """
    if on_progress is None:

        def on_progress(msg):
            return logger.info("research: %s", msg)

    try:
        from crewai import Agent, Crew, Task
        from crewai.tools import tool as crew_tool
    except ImportError:
        logger.error("crewai not installed — cannot run autonomous research")
        return "⚠️ CrewAI is not installed. Autonomous research unavailable."

    on_progress("Initialising research tools…")

    # ── Tools ──────────────────────────────────────────────────────────
    @crew_tool("Search the public web for current information")
    def web_search(query: str, max_results: int = 5) -> str:
        """Search the web via Tavily. Returns ranked results with snippets."""
        import httpx

        from ..config import settings

        try:
            resp = httpx.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": settings.tavily_api_key,
                    "query": query,
                    "max_results": max_results,
                    "search_depth": "advanced",
                },
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results", [])
            lines = []
            for r in results:
                lines.append(f"**{r.get('title', 'Untitled')}**\n{r.get('url', '')}\n{r.get('content', '')[:500]}\n")
            answer = data.get("answer", "")
            if answer:
                lines.insert(0, f"## Synthesised Answer\n{answer}\n")
            return "\n".join(lines) if lines else "No results found."
        except Exception as e:
            return f"Search error: {e}"

    @crew_tool("Fetch and read the full content of a web page")
    def web_extract(urls: str) -> str:
        """Extract cleaned text from one or more URLs (comma-separated)."""
        import httpx

        from ..config import settings

        url_list = [u.strip() for u in urls.split(",") if u.strip()]
        try:
            resp = httpx.post(
                "https://api.tavily.com/extract",
                json={
                    "api_key": settings.tavily_api_key,
                    "urls": url_list,
                },
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results", [])
            if data.get("failed_results"):
                for fr in data["failed_results"]:
                    logger.warning("Tavily extract failed: %s", fr.get("url"))
            lines = []
            for r in results:
                lines.append(f"## {r.get('url', '')}\n{r.get('raw_content', '')[:5000]}\n")
            return "\n".join(lines) if lines else "No content extracted."
        except Exception as e:
            return f"Extract error: {e}"

    @crew_tool("Read a file from the DAO context repository")
    def read_context_file(path: str) -> str:
        """Read a file from agentic_ai_context."""
        from ..context import get_context_file

        content = get_context_file(path)
        return content if content else f"File not found: {path}"

    @crew_tool("Read a file from a GitHub repository")
    def read_repo_file(repo: str, path: str) -> str:
        """Read a file from any TrueSightDAO GitHub repository via the Contents API."""
        import httpx

        try:
            resp = httpx.get(
                f"https://raw.githubusercontent.com/TrueSightDAO/{repo}/main/{path}",
                timeout=15.0,
            )
            if resp.status_code == 200:
                return resp.text[:10000]
            return f"Could not read {repo}/{path}: HTTP {resp.status_code}"
        except Exception as e:
            return f"Read error: {e}"

    # ── Agent ──────────────────────────────────────────────────────────
    from ..roles import ROLES

    role_def = ROLES.get(role)
    role_name = role_def.name if role_def else role
    role_goal = (
        f"Conduct exhaustive research on: {topic}. "
        f"Use web_search to find current data, web_extract to read full articles, "
        f"read_context_file for DAO strategy docs, and read_repo_file for existing content. "
        f"Synthesise all findings into a comprehensive, well-structured markdown report. "
        f"The report must be self-contained — a reader should understand the full picture "
        f"without reading the source material."
    )

    researcher = Agent(
        role=role_name,
        goal=role_goal,
        backstory=f"You are an expert {role_name} for TrueSight DAO and Agroverse. "
        f"You have deep knowledge of the ceremonial cacao market, "
        f"holistic wellness industry, and content marketing strategy.",
        tools=[web_search, web_extract, read_context_file, read_repo_file],
        llm="deepseek/deepseek-chat",
        verbose=True,
        max_iter=20,
        allow_delegation=False,
    )

    # ── Task ───────────────────────────────────────────────────────────
    task_description = (
        f"Research topic: {topic}\n\n"
        f"Steps:\n"
        f"1. Use web_search to find market data, demographics, trends, and competitor strategies.\n"
        f"2. Use web_extract to read the most promising articles in full.\n"
        f"3. Use read_context_file to pull in DAO docs (GROWTH_MODEL.md, CMO_SETH_GODIN.md, etc.) for strategy alignment.\n"
        f"4. Use read_repo_file to check existing content in relevant repos.\n"
        f"5. Synthesise everything into a comprehensive markdown report with these sections:\n"
        f"   - Executive Summary\n"
        f"   - Market Overview & Size\n"
        f"   - Consumer Demographics & Psychographics\n"
        f"   - Competitor Landscape\n"
        f"   - Content Strategy Recommendations\n"
        f"   - SEO & Keyword Opportunities\n"
        f"   - Action Plan & Next Steps\n"
        f"6. The final report must be self-contained, well-sourced, and actionable.\n"
        f"7. Do NOT stop until all sections are complete and well-researched."
    )

    task = Task(
        description=task_description,
        expected_output="A comprehensive, well-structured markdown research report with all sections complete.",
        agent=researcher,
    )

    # ── Crew ───────────────────────────────────────────────────────────
    crew = Crew(
        agents=[researcher],
        tasks=[task],
        verbose=True,
        step_callback=lambda step: (
            on_progress(f"Research step: {step.get('tool', 'thinking')} — {str(step.get('output', ''))[:100]}")
            if step
            else None
        ),
    )

    on_progress(f"Starting research on: {topic}")
    result = crew.kickoff()

    output = str(result) if result else "(no output)"

    # ── Commit to GitHub repo ─────────────────────────────────────────
    try:
        import re as _re

        slug = _re.sub(r"[^a-z0-9]+", "_", topic.lower().strip())[:50]
        file_path = f"{slug}.md"
        _commit_to_github(target_repo, file_path, output, f"research: {topic}")
        on_progress(f"Report committed to {target_repo}/{file_path}")
    except Exception as e:
        logger.warning("Could not commit report to %s: %s", target_repo, e)
        on_progress(f"Report ready but could not commit to GitHub: {e}")

    return output


def _commit_to_github(repo: str, path: str, content: str, message: str) -> None:
    """Commit a file to a GitHub repo via the Contents API."""
    import base64
    import os

    import httpx

    token = os.getenv("TRUESIGHT_DAO_AUTOPILOT", "")
    if not token:
        raise RuntimeError("TRUESIGHT_DAO_AUTOPILOT not set")

    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }

    # Check if file exists (need sha for update)
    check_url = f"https://api.github.com/repos/TrueSightDAO/{repo}/contents/{path}"
    sha = None
    try:
        r = httpx.get(check_url, headers=headers, timeout=10.0)
        if r.status_code == 200:
            sha = r.json().get("sha")
    except Exception:
        pass

    payload = {
        "message": message,
        "content": encoded,
    }
    if sha:
        payload["sha"] = sha

    resp = httpx.put(check_url, headers=headers, json=payload, timeout=20.0)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"GitHub API returned {resp.status_code}: {resp.text[:300]}")
    logger.info("Committed %s to TrueSightDAO/%s", path, repo)


def run_research_background(
    role: str,
    topic: str,
    target_repo: str,
    on_progress: Callable[[str], None],
    on_done: Callable[[str], None],
) -> None:
    """Run research in a background thread, calling callbacks for progress and completion."""

    def _run():
        try:
            result = run_research(role, topic, target_repo, on_progress)
            on_done(result)
        except Exception as e:
            logger.exception("Background research failed")
            on_done(f"⚠️ Research failed: {e}")

    threading.Thread(target=_run, daemon=True, name=f"research-{role}").start()
