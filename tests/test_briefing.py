"""Tests for the escalation handoff brief.

The brief is the one place an LLM writes operator-facing text about a live
incident, so the tests pin two things: the deterministic writer says the right
thing in each completion state, and the LLM path can never turn a failure into
silence or a hallucination into an instruction.
"""

import briefing
import escalation as esc
import pytest

from test_escalation import default_trains, snapshot


def graded(closed=("T2",), trains=None, dispatch=None, now=1000.0):
    """Run one incident through the ledger and return (payload, incident)."""
    ledger = esc.IncidentLedger()
    snap = snapshot(closed=list(closed), trains=trains)
    ledger.observe(snap, now=now)
    if dispatch:
        ledger.record_dispatch("A", dispatch, snap, now=now + 1)
    payload = ledger.observe(snap, now=now + 2)
    return payload, payload["incidents"][0]


def sections(text):
    return {line.split(":", 1)[0]: line.split(":", 1)[1].strip()
            for line in text.splitlines() if ":" in line}


# ── shape ───────────────────────────────────────────────────────────────────

def test_template_brief_has_exactly_the_four_sections():
    payload, incident = graded()
    text = briefing.template_brief(briefing.build_context(payload, incident))
    lines = text.splitlines()
    assert [line.split(":")[0] for line in lines] == ["SITUATION", "IMPACT", "DONE", "ASK"]
    assert "" not in lines


def test_brief_names_the_corridor_and_the_receiving_owner():
    payload, incident = graded()
    text = briefing.template_brief(briefing.build_context(payload, incident))
    assert incident["corridor"] in text
    assert incident["owner"] in text


# ── completion states drive the wording ─────────────────────────────────────

def test_unresolved_brief_says_nothing_is_verified():
    payload, incident = graded()
    assert incident["resolution"] == esc.UNRESOLVED
    text = briefing.template_brief(briefing.build_context(payload, incident))
    assert "nothing verified complete yet" in sections(text)["DONE"].lower()
    assert "approve and execute" in sections(text)["ASK"]


def test_partial_brief_reports_both_counts():
    payload, incident = graded(
        dispatch=[{"strategy_id": "T_CLOSE_T2", "track_id": "T2"},
                  {"strategy_id": "R_REROUTE_TR1", "train_id": "TR1",
                   "old_route": ["A", "B", "C"], "new_route": ["A", "C"]}])
    assert incident["resolution"] == esc.PARTIAL
    text = briefing.template_brief(briefing.build_context(payload, incident))
    assert "1/2 committed action(s) verified" in sections(text)["DONE"]
    assert "Reroute TR1" in sections(text)["ASK"]


def test_blocked_brief_demands_restoration_and_never_promises_another_plan():
    payload, incident = graded(closed=["T5"])
    assert incident["resolution"] == esc.BLOCKED
    text = briefing.template_brief(briefing.build_context(payload, incident))
    ask = sections(text)["ASK"]
    assert "authorise physical restoration" in ask
    # The reason may say "not another plan" — a refusal, not a promise. What
    # must never appear is language that reads as recovery under way.
    assert "not another plan" in ask
    for forbidden in ("in progress", "re-run", "will be resolved",
                      "approve an alternative plan", "keep ownership until"):
        assert forbidden not in text.lower()


def test_complete_brief_asks_for_stand_down():
    trains = default_trains()
    trains["TR1"]["route"] = ["A", "C"]
    ledger = esc.IncidentLedger()
    ledger.observe(snapshot(closed=["T2"]), now=1000.0)
    payload = ledger.observe(snapshot(trains=trains), now=1001.0)
    incident = payload["incidents"][0]
    assert incident["resolution"] == esc.COMPLETE
    text = briefing.template_brief(briefing.build_context(payload, incident))
    assert "confirm stand-down" in sections(text)["ASK"]


def test_impact_line_lists_stranded_trains_by_id():
    trains = default_trains()
    trains["TR1"]["held"] = True
    payload, incident = graded(trains=trains)
    text = briefing.template_brief(briefing.build_context(payload, incident))
    assert "TR1 held" in sections(text)["IMPACT"]
    assert "300 passengers at risk" in sections(text)["IMPACT"]


# ── context passed to the model ─────────────────────────────────────────────

def test_context_carries_only_verified_work_as_done():
    payload, incident = graded(
        dispatch=[{"strategy_id": "T_CLOSE_T2", "track_id": "T2"},
                  {"strategy_id": "R_REROUTE_TR1", "train_id": "TR1",
                   "old_route": ["A", "B", "C"], "new_route": ["A", "C"]}])
    context = briefing.build_context(payload, incident)
    assert context["committed_actions"]["verified_done"] == ["Close T2"]
    assert context["committed_actions"]["still_pending"] == ["Reroute TR1"]
    assert context["resolution"] == esc.PARTIAL
    assert context["escalating_to"] == incident["owner"]


def test_context_explains_why_the_incident_sits_at_this_level():
    payload, incident = graded(closed=["T5"])
    context = briefing.build_context(payload, incident)
    assert any("No alternate corridor" in reason for reason in context["why_this_level"])


# ── the LLM path ────────────────────────────────────────────────────────────

def test_no_api_key_yields_the_template(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    payload, incident = graded()
    result = briefing.handoff_brief(payload, incident)
    assert result["source"] == "rules"
    assert result["text"].startswith("SITUATION:")


def test_llm_text_is_used_when_the_call_succeeds(monkeypatch):
    monkeypatch.setattr(briefing, "llm_brief", lambda ctx: "SITUATION: written by the model")
    payload, incident = graded()
    result = briefing.handoff_brief(payload, incident)
    assert result["source"] == "llm"
    assert result["text"] == "SITUATION: written by the model"


def test_llm_failure_falls_back_silently(monkeypatch):
    class _Boom:
        class Anthropic:
            def __init__(self, **_kwargs):
                raise RuntimeError("network down")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(briefing, "anthropic", _Boom)
    payload, incident = graded()
    result = briefing.handoff_brief(payload, incident)
    assert result["source"] == "rules"
    assert "SITUATION" in result["text"]


def test_llm_refusal_falls_back(monkeypatch):
    class _Response:
        stop_reason = "refusal"
        content = []

    class _Client:
        def __init__(self, **_kwargs):
            self.messages = self

        def create(self, **_kwargs):
            return _Response()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(briefing, "anthropic", type("M", (), {"Anthropic": _Client}))
    payload, incident = graded()
    assert briefing.handoff_brief(payload, incident)["source"] == "rules"


def test_llm_receives_the_grounded_context(monkeypatch):
    captured = {}

    class _Block:
        type = "text"
        text = "SITUATION: ok\nIMPACT: ok\nDONE: ok\nASK: ok"

    class _Response:
        stop_reason = "end_turn"
        content = [_Block()]

    class _Client:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs
            self.messages = self

        def create(self, **kwargs):
            captured["call"] = kwargs
            return _Response()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(briefing, "anthropic", type("M", (), {"Anthropic": _Client}))
    payload, incident = graded(closed=["T5"])
    briefing.handoff_brief(payload, incident)

    # A slow brief must never outlive the console's patience, and a retry storm
    # would multiply that wait.
    assert captured["client_kwargs"]["timeout"] == briefing.BRIEF_TIMEOUT_S
    assert captured["client_kwargs"]["max_retries"] == 0
    sent = captured["call"]["messages"][0]["content"]
    assert "INC-001" in sent and "BLOCKED" in sent
    assert captured["call"]["system"] is briefing.SYSTEM_PROMPT


# ── prompt guardrails ───────────────────────────────────────────────────────

def test_system_prompt_keeps_its_grounding_rules():
    """These clauses are load-bearing; losing one in an edit changes behaviour
    in ways no other test would catch."""
    prompt = briefing.SYSTEM_PROMPT
    for clause in ("Use ONLY facts present in the JSON payload",
                   "Never invent",
                   "not reported",
                   "SITUATION:", "IMPACT:", "DONE:", "ASK:",
                   "UNRESOLVED", "PARTIAL", "BLOCKED", "COMPLETE",
                   "90 words or fewer"):
        assert clause in prompt, f"missing prompt guardrail: {clause}"


def test_system_prompt_forbids_softening_a_blocked_incident():
    assert "never soften this into" in briefing.SYSTEM_PROMPT.lower()
