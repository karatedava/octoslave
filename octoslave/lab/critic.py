"""
The Critic (Scientific Skeptic) agent.

The Critic always exists alongside the team. Its job is to challenge a proposal
BEFORE the lab spends effort implementing it — catching flawed assumptions,
unfalsifiable claims, wasted-effort plans, and missing controls. It returns a
structured verdict the runner uses as a gate.
"""

from __future__ import annotations

from openai import OpenAI

from .llm import complete_json
from .state import LabSession


_CRITIC_SYSTEM = (
    "You are the Critic — the lab's scientific skeptic. You are sharp, fair, and "
    "constructive. You challenge plans before they consume effort: surface "
    "unstated assumptions, weak reasoning, unfalsifiable or unverifiable claims, "
    "missing baselines/controls, and 'busy-work' that won't move the goal. You "
    "are domain-agnostic. You do NOT rubber-stamp; but you also do not block "
    "sound, concrete plans. Be specific and brief. You have NO tools in this "
    "step — do not call tools or emit any tool-call syntax; judge only from the "
    "information provided."
)

# Verdicts.
APPROVE = "approve"
REVISE = "revise"
REJECT = "reject"


def critique(session: LabSession, client: OpenAI, proposal: str,
             context: str = "") -> dict:
    """Challenge a proposal. Returns:
    {"verdict": "approve"|"revise"|"reject",
     "concerns": [str, ...], "suggestions": [str, ...], "summary": str}."""
    user = f"""\
Task: {session.task}

Proposed plan / design to scrutinize:
{proposal[:18000]}

{('Additional context:\\n' + context[:6000]) if context else ''}

Challenge this proposal before the team implements it. Decide a verdict:
- "approve": concrete, sound, worth doing now.
- "revise": promising but has fixable problems — list them.
- "reject": fundamentally flawed or a waste of effort — explain why.

Respond as JSON:
{{"verdict": "approve|revise|reject",
  "concerns": ["..."],
  "suggestions": ["..."],
  "summary": "<2-3 sentence bottom line>"}}"""
    data = complete_json(client, session.model, _CRITIC_SYSTEM, user, max_tokens=1200)
    if not isinstance(data, dict):
        # Fail open: don't let a parse failure block the lab, but flag it.
        return {"verdict": APPROVE, "concerns": [],
                "suggestions": ["Critic response could not be parsed."],
                "summary": "Critic unavailable; proceeding."}
    verdict = str(data.get("verdict", APPROVE)).strip().lower()
    if verdict not in (APPROVE, REVISE, REJECT):
        verdict = APPROVE
    return {
        "verdict": verdict,
        "concerns": [str(c) for c in (data.get("concerns") or [])][:10],
        "suggestions": [str(s) for s in (data.get("suggestions") or [])][:10],
        "summary": str(data.get("summary", "")),
    }
