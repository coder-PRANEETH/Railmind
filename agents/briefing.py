"""briefing.py — the escalation handoff brief.

When an incident moves up the ladder, somebody new inherits it. This module
writes the note that travels with it: what happened, who it hits, what has
already been proved done on the twin, and what the receiving controller must
decide now.

Two writers, one contract:
  * `llm_brief()`   — the language model, when ANTHROPIC_API_KEY is set.
                      Grounded hard: it may only restate facts in the payload.
  * `template_brief()` — deterministic prose from the same numbers, same four
                      sections. Always available, and always the fallback.

The offline-first invariant holds: a missing key, a timeout, a refusal or any
exception yields the template. The caller is told which writer produced the
text so the console can label it honestly.
"""

from __future__ import annotations

import json
import os
from typing import Optional

try:
    import anthropic
except ImportError:  # optional dependency — the template path never needs it
    anthropic = None

from escalation import (
    BLOCKED,
    COMPLETE,
    PARTIAL,
    UNRESOLVED,
    WORK_BLOCKED,
    WORK_DONE,
    WORK_FAILED,
)

BRIEF_MODEL = "claude-opus-5"
BRIEF_TIMEOUT_S = 8.0
BRIEF_MAX_TOKENS = 700

# ─────────────────────────────────────────────────────────────────────────────
# PROMPT
#
# Written for one job: a controller who has just been handed an incident reads
# this once, on a screen, while trains are moving. Every rule below exists to
# stop a specific failure mode seen in generic LLM incident prose:
#
#   • invented specifics  → "USE ONLY the facts in the payload" + explicit
#                           "not reported" escape hatch
#   • false reassurance   → per-state directives; BLOCKED and UNRESOLVED are
#                           forbidden from sounding like progress
#   • unowned asks        → the ASK line must name the receiving role and one
#                           decision, not a list of aspirations
#   • padding             → fixed four-line shape, hard word budget, banned
#                           opener phrases
#   • invented remedies   → may only reference actions already in the payload
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are RailMind's escalation desk. You write the handoff brief that travels \
with a railway incident when it moves to a higher handling level.

READER
The brief is addressed to the receiving controller named in `escalating_to`. \
They are taking ownership right now, they have not seen this incident before, \
and they will read it once on a control-room screen while trains are moving.

OUTPUT SHAPE — exactly four lines, each starting with its label, no blank \
lines, no markdown, no preamble, no sign-off:
SITUATION: what failed, where, and how long it has been open.
IMPACT: which trains and how many passengers are affected, and their current \
disposition.
DONE: which committed actions are verified complete on the digital twin.
ASK: the single decision or action you need from the receiving controller now.

GROUNDING — non-negotiable
- Use ONLY facts present in the JSON payload. Never invent train numbers, \
station names, timings, causes, weather, casualties, resources or policies.
- Reproduce identifiers and numbers exactly as given (train ids, track ids, \
counts, minutes, tier codes).
- If something a line would normally carry is absent from the payload, write \
"not reported" rather than estimating it.
- In ASK, you may only reference actions that already exist in the payload's \
work items or the recommended plan. Do not invent new remedies.
- State no cause the payload does not state. A closed track is a closed track; \
you do not know why it closed.

COMPLETION SIGNAL — the payload's `resolution` is authoritative. Match your \
tone to it and never overstate:
- UNRESOLVED: nothing has landed yet. Say so plainly. DONE reads "Nothing \
verified complete yet."
- PARTIAL: some work is verified, some is not. Give both counts. Do not imply \
the incident is contained.
- BLOCKED: automated recovery is exhausted and a physical intervention is \
required. Never suggest that another plan or a re-run will fix it, and never \
soften this into "in progress".
- COMPLETE: everything is verified. ASK becomes the stand-down confirmation \
the controller should give.

VOICE
Control-room register: flat, specific, no adjectives of alarm, no hedging, no \
apology, no reassurance. Active voice. Present tense for current state, past \
tense only for verified completed work. Never open with "This is", "I am \
writing", "Please be advised", "As you know" or any restatement of the task. \
Total length 90 words or fewer across all four lines."""


USER_PROMPT_HEADER = """\
Write the handoff brief for the escalation described in this payload.

Escalation payload (JSON):
"""


def build_context(escalation: dict, incident: dict) -> dict:
    """Compact, self-describing facts for the model — no derived opinions.

    Only fields the brief may cite are included: anything absent here is
    supposed to be absent, and the prompt's "not reported" rule covers it.
    """
    progress = incident.get("progress", {})
    dispositions = incident.get("dispositions", {})
    items = incident.get("work_items", [])

    return {
        "incident_id": incident.get("id"),
        "corridor": incident.get("corridor"),
        "failure_kind": incident.get("kind"),
        "open_for_minutes": round(incident.get("age_seconds", 0) / 60.0, 1),
        "handling_level_now": f"{incident.get('code')} {incident.get('tier_label')}",
        "escalating_to": incident.get("owner"),
        "acknowledged_by": incident.get("acknowledged_by") or "nobody yet",
        "resolution": incident.get("resolution"),
        "resolution_reason": incident.get("resolution_reason"),
        "why_this_level": [d.get("detail") for d in incident.get("drivers", [])],
        "trains": {
            "affected": incident.get("affected_trains", []),
            "disposition_by_train": dispositions,
            "recovered": progress.get("trains_recovered", 0),
            "total": progress.get("trains_total", 0),
        },
        "passengers_at_risk": incident.get("passengers_at_risk", 0),
        "committed_actions": {
            "verified_done": [i["label"] for i in items if i.get("state") == WORK_DONE],
            "still_pending": [i["label"] for i in items
                              if i.get("state") not in (WORK_DONE, WORK_FAILED, WORK_BLOCKED)],
            "failed": [i["label"] for i in items if i.get("state") == WORK_FAILED],
            "blocked": [i["label"] for i in items if i.get("state") == WORK_BLOCKED],
            "done_count": progress.get("actions_done", 0),
            "total_count": progress.get("actions_total", 0),
        },
        "network": {
            "open_incidents": escalation.get("totals", {}).get("open", 0),
            "network_level": f"{escalation.get('code')} {escalation.get('label')}",
            "station_pairs_with_no_route": escalation.get("severed_pairs", 0),
        },
    }


def llm_brief(context: dict) -> Optional[str]:
    """Model-written brief, or None if unavailable, slow, refusing or failing."""
    if anthropic is None or not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        client = anthropic.Anthropic(timeout=BRIEF_TIMEOUT_S, max_retries=0)
        response = client.messages.create(
            model=BRIEF_MODEL,
            max_tokens=BRIEF_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": USER_PROMPT_HEADER + json.dumps(context, indent=2),
            }],
        )
        if response.stop_reason == "refusal":
            return None
        text = next((b.text for b in response.content if b.type == "text"), None)
        return text.strip() if text else None
    except Exception:
        return None


def template_brief(context: dict) -> str:
    """Deterministic brief. Same four sections, same facts, no model required."""
    trains = context["trains"]
    actions = context["committed_actions"]
    resolution = context["resolution"]

    situation = (
        f"SITUATION: {context['corridor']} is out of service"
        if context["failure_kind"] == "TRACK_FAILURE"
        else f"SITUATION: severe weather on {context['corridor']}"
    )
    situation += (
        f"; open {context['open_for_minutes']:.1f} min at {context['handling_level_now']}, "
        f"acknowledged by {context['acknowledged_by']}."
    )

    if trains["total"]:
        held = [t for t, d in trains["disposition_by_train"].items() if d == "HELD"]
        exposed = [t for t, d in trains["disposition_by_train"].items() if d == "EXPOSED"]
        no_path = [t for t, d in trains["disposition_by_train"].items() if d == "BLOCKED"]
        detail = []
        if held:
            detail.append(f"{', '.join(sorted(held))} held")
        if exposed:
            detail.append(f"{', '.join(sorted(exposed))} still routed into it")
        if no_path:
            detail.append(f"{', '.join(sorted(no_path))} with no alternate corridor")
        impact = (
            f"IMPACT: {trains['recovered']}/{trains['total']} affected train(s) recovered, "
            f"{context['passengers_at_risk']} passengers at risk"
            + (f"; {'; '.join(detail)}." if detail else ".")
        )
    else:
        impact = "IMPACT: no train is currently routed across the failure."

    if actions["verified_done"]:
        done = (f"DONE: {actions['done_count']}/{actions['total_count']} committed action(s) "
                f"verified on the twin — {', '.join(actions['verified_done'])}.")
    else:
        done = "DONE: nothing verified complete yet."
    if actions["failed"]:
        done += f" Failed: {', '.join(actions['failed'])}."

    if resolution == BLOCKED:
        ask = (f"ASK: {context['escalating_to']} to authorise physical restoration — "
               f"{context['resolution_reason']}")
    elif resolution == COMPLETE:
        ask = (f"ASK: {context['escalating_to']} to confirm stand-down; "
               "all committed work is verified on the twin.")
    elif resolution == PARTIAL:
        pending = ", ".join(actions["still_pending"]) or "the outstanding actions"
        ask = (f"ASK: {context['escalating_to']} to keep ownership until {pending} "
               "complete, or approve an alternative plan.")
    else:  # UNRESOLVED
        ask = (f"ASK: {context['escalating_to']} to approve and execute a recovery plan now — "
               "no committed action has landed.")

    return "\n".join([situation, impact, done, ask])


def handoff_brief(escalation: dict, incident: dict, use_llm: bool = True) -> dict:
    """Return {text, source, incident_id, resolution, level} for one incident."""
    context = build_context(escalation, incident)
    text = llm_brief(context) if use_llm else None
    source = "llm" if text else "rules"
    return {
        "incident_id": incident.get("id"),
        "resolution": incident.get("resolution", UNRESOLVED),
        "level": incident.get("code"),
        "escalating_to": incident.get("owner"),
        "text": text or template_brief(context),
        "source": source,
    }
