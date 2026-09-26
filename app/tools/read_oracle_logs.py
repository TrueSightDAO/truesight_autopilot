"""Tool to read oracle draw logs from the lineage-credentials repo.

CANONICAL SOURCE (system of record) for Gary Teh's oracle draws:
    TrueSightDAO/lineage-credentials
    programs/truesight-grounding/pk-iWL9OH9hpE_D/practice/

Gary's daily oracle draws ([PRACTICE EVENT] of type `oracle-consultation`)
are recorded there as JSON files named `YYYY-MM-DDTHHMMSSmmmZ-<id>.json`.

Do NOT use TrueSightDAO/oracle_logs -- that repo is STALE (last draw
2026-06-03) and explicitly non-authoritative. See
agentic_ai_context/oracle/GOVERNOR_ORACLE_SOURCE.md.
"""

from __future__ import annotations

import json
import logging

import httpx

logger = logging.getLogger("autopilot.oracle_logs")

PRACTICE_REPO = "TrueSightDAO/lineage-credentials"
PRACTICE_PATH = "programs/truesight-grounding/pk-iWL9OH9hpE_D/practice"
PRACTICE_BASE = (
    f"https://raw.githubusercontent.com/{PRACTICE_REPO}/main/{PRACTICE_PATH}"
)
API_LIST_URL = f"https://api.github.com/repos/{PRACTICE_REPO}/contents/{PRACTICE_PATH}"


def _list_practice_files() -> list[str]:
    """Return practice filenames (.json), sorted ascending by timestamp prefix."""
    resp = httpx.get(
        API_LIST_URL, headers={"Accept": "application/vnd.github+json"}, timeout=10.0
    )
    resp.raise_for_status()
    files = resp.json()
    if not isinstance(files, list):
        return []
    names = [f["name"] for f in files if f.get("name", "").endswith(".json")]
    names.sort()
    return names


def read_oracle_logs(date: str | None = None) -> str:
    """Read oracle draw logs from the lineage-credentials practice history.

    If date is None, returns a listing of available draw days.
    If date is "latest", fetches the most recent draw.
    Otherwise, fetches the draw(s) for YYYY-MM-DD.

    Returns JSON string with status and content.
    """
    try:
        names = _list_practice_files()
        if not names:
            return json.dumps(
                {"status": "ok", "draws": [], "message": "No draws found"}
            )

        if date is None:
            days = sorted({n[:10] for n in names}, reverse=True)
            return json.dumps(
                {
                    "status": "ok",
                    "draws": days,
                    "message": (
                        f"{len(days)} draw days available "
                        "(lineage-credentials practice history)"
                    ),
                }
            )

        if date == "latest":
            fname = names[-1]
            day = fname[:10]
        else:
            matches = [n for n in names if n.startswith(date)]
            if not matches:
                return json.dumps(
                    {
                        "status": "error",
                        "message": (
                            f"Draw not found for {date} in "
                            "lineage-credentials practice history"
                        ),
                    }
                )
            fname = matches[-1]
            day = date

        url = f"{PRACTICE_BASE}/{fname}"
        resp = httpx.get(url, timeout=10.0)
        if resp.status_code != 200:
            return json.dumps({"status": "error", "message": f"Draw not found: {day}"})

        return json.dumps(
            {
                "status": "ok",
                "date": day,
                "filename": fname,
                "content": resp.text,
                "source": f"{PRACTICE_REPO}/{PRACTICE_PATH}/{fname}",
                "message": (
                    f"Oracle draw for {day} retrieved from lineage-credentials"
                ),
            }
        )

    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


# ---------------------------------------------------------------------------
# capability manifest entry
# ---------------------------------------------------------------------------

from ..tool_registry import ToolSpec  # noqa: E402

TOOL_SPEC = ToolSpec(
    name="read_oracle_logs",
    description=(
        "Read oracle draw logs from the lineage-credentials practice history "
        "(TrueSightDAO/lineage-credentials programs/truesight-grounding/"
        "pk-iWL9OH9hpE_D/practice/). This is the CANONICAL source for Gary's "
        "daily oracle draws. Do NOT use the oracle_logs repo -- it is stale "
        "(last draw 2026-06-03) and non-authoritative."
    ),
    parameters={
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "description": "YYYY-MM-DD date, 'latest', or omit to list draws.",
                "default": "latest",
            }
        },
    },
    handler=lambda args, ctx: read_oracle_logs(date=args.get("date")),
)
