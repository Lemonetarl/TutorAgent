"""
test_solver_pipeline.py -- run with:  pytest test_solver_pipeline.py -v
Every scenario scripts ONLY the LLM messages; concurrency, voting, the tool
loop, adjudication, the gate, and the review queue all execute for real.

The demo problem: a ball dropped from rest, h = 20 m, g = 9.8 m/s^2.
    t = sqrt(2h/g) = 2.0203... s        the factor-2 slip gives 4.04 s
"""
import asyncio
import json
from xmlrpc import client

import pytest

from scripts.models import AnswerValue, ProblemOnly, StudentAnswer
from scripts.solver_pipeline import (Adjudication, FakeMessage, FakeUsage, MockClient,
                             PipelineResult, REVIEW_QUEUE_PATH, ToolUseBlock,
                             execute_tool, run_pipeline)

PROBLEM = ProblemOnly(
    subject_domain="physics",
    statement="A ball is dropped from rest from a height of 20 m. "
              "Taking g = 9.8 m/s**2, how long does it take to reach the ground?",
    is_complete=True, has_multiple_parts=False)

QUARANTINE = StudentAnswer(answer_text="I got t = 2.0 s",
                           values=[AnswerValue(variable="t", sympy_form="2.0")],
                           units="s")


# ------------------------- scripted-message builders -----------------------

def solution_dict(value: str, method: str) -> dict:
    return {"given": ["h = 20 m", "g = 9.8 m/s**2", "v0 = 0"],
            "unknowns": ["t"],
            "method_name": method,
            "steps": [
                {"id": 1, "action": "write the displacement relation",
                 "equation": "h - g*t**2/2", "result": "t**2 - 2*h/g"},
                {"id": 2, "action": "solve for t",
                 "equation": "sqrt(2*h/g)", "result": value}],
            "final_answer": {"value": value, "unit": "s",
                             "symbolic_form": "sqrt(2*h/g)",
                             "answer_type": "scalar",
                             "additional_values": []}}


def solver_msg(value: str, method: str = "kinematics") -> FakeMessage:
    return FakeMessage(content=[ToolUseBlock(
        id="tu_s", name="emit_solution", input=solution_dict(value, method))])


def verifier_msgs(value: str, method: str = "energy_methods") -> list[FakeMessage]:
    """Two turns: a real tool call (exercising the loop + dispatcher), then
    the submission."""
    return [
        FakeMessage(content=[ToolUseBlock(
            id="tu_v1", name="evaluate_numeric",
            input={"expr": "sqrt(2*h/g)", "substitutions": {"h": 20, "g": 9.8}})]),
        FakeMessage(content=[ToolUseBlock(
            id="tu_v2", name="submit_solution",
            input=solution_dict(value, method))]),
    ]


def adjudicator_msg(contradiction: str) -> FakeMessage:
    return FakeMessage(content=[ToolUseBlock(
        id="tu_a", name="emit_adjudication",
        input={"first_divergent_solver_step": 2,
               "first_divergent_verifier_step": 2,
               "contradiction": contradiction,
               "suspected_faulty": "unclear"})])


def run(client) -> "PipelineResult":
    return asyncio.run(run_pipeline(client, PROBLEM, quarantine=QUARANTINE, k=3))

# --------------------------------- scenarios -------------------------------

def test_unanimous_first_round_is_HIGH():
    script = [solver_msg("2.02")] * 3 + verifier_msgs("2.0203")
    r = run(MockClient(script))
    assert r.status == "HIGH" and not r.blocked
    assert r.rounds_used == 1 and r.solver_votes == 3
    assert r.reference is not None
    assert r.reference.values[0].sympy_form == "2.02"
    assert r.verifier_method == "energy_methods"


def test_split_vote_with_verifier_agreement_is_MEDIUM():
    # 2 of 3 samples agree; the third made the classic factor-2 slip.
    script = ([solver_msg("2.02"), solver_msg("2.02"), solver_msg("4.04")]
              + verifier_msgs("2.02"))
    r = run(MockClient(script))
    assert r.status == "MEDIUM" and not r.blocked
    assert r.solver_votes == 2 and r.solver_k == 3


def test_divergence_recovered_via_evidence_retry_is_MEDIUM():
    contradiction = ("Solution A: t = sqrt(2h/g) = 2.02 s. Solution B: "
                     "t = sqrt(h/g) = 1.43 s. Substituting t = 1.43 s into "
                     "h = g*t^2/2 gives 10.0 m, not 20 m; B's relation "
                     "drops the factor of 2.")
    script = (
        # round 1: unanimous solver vs a wrong verifier -> adjudication
        [solver_msg("2.02")] * 3 + verifier_msgs("1.43", "energy_methods")
        + [adjudicator_msg(contradiction)]
        # round 2 (evidence-seeded): both converge
        + [solver_msg("2.02")] * 3 + verifier_msgs("2.02", "dimensional_analysis"))
    client = MockClient(script)
    r = run(client)
    assert r.status == "MEDIUM" and r.rounds_used == 2 and not r.blocked
    # the evidence reached round 2 -- and reasoning chains did NOT
    round2_call = client.calls[6]  # calls 0-5 = round 1, call 6 = first round-2 solve
    prompt = round2_call["messages"][0]["content"]
    assert "drops the factor of 2" in prompt          # evidence crossed over
    assert "h - g*t**2/2" not in prompt               # step equations did not


def test_persistent_divergence_is_UNCERTAIN_blocked_and_queued():
    before = (REVIEW_QUEUE_PATH.read_text().count("\n")
              if REVIEW_QUEUE_PATH.exists() else 0)
    one_round = ([solver_msg("2.02")] * 3 + verifier_msgs("4.04")
                 + [adjudicator_msg("A gives 2.02 s, B gives 4.04 s; "
                                    "B's substitute-back yields h = 80 m.")])
    r = run(MockClient(one_round * 3))
    assert r.status == "UNCERTAIN" and r.blocked
    assert r.reference is None and r.rounds_used == 3
    after = REVIEW_QUEUE_PATH.read_text().count("\n")
    assert after == before + 1
    row = json.loads(REVIEW_QUEUE_PATH.read_text().splitlines()[-1])
    assert row["reason"].startswith("no solver<->verifier convergence")


def test_I1_leakage_tripwire_fires_before_any_api_call():
    leaked = ProblemOnly(
        subject_domain="physics",
        statement="A ball is dropped from 20 m. How long? The student got 2.0 s.",
        is_complete=True, has_multiple_parts=False)
    client = MockClient([])          # zero scripted messages: none may be made
    with pytest.raises(ValueError, match="ISOLATION BREACH"):
        asyncio.run(run_pipeline(client, leaked, quarantine=QUARANTINE, k=3))
    assert client.calls == []        # tripwire fired BEFORE the model was called


def test_I2_blindness_tripwire_fires():
    from scripts.solver_pipeline import enforce_verifier_blindness, StructuredSolution
    sol = StructuredSolution.model_validate(solution_dict("2.02", "kinematics"))
    with pytest.raises(ValueError, match="BLINDNESS BREACH"):
        enforce_verifier_blindness(
            "verifier prompt accidentally containing h - g*t**2/2 from the solver",
            [sol])


# ------------------------------ the tools themselves -----------------------

def test_tools_run_real_math():
    eq = execute_tool("check_symbolic_equivalence",
                      {"expr_a": "sqrt(2*h/g)", "expr_b": "(2*h/g)**(1/2)"})
    assert eq["equivalent"] is True

    num = execute_tool("evaluate_numeric",
                       {"expr": "sqrt(2*h/g)", "substitutions": {"h": 20, "g": 9.8}})
    assert num["ok"] and abs(num["value"] - 2.0203) < 1e-3

    units = execute_tool("check_units", {"unit_str": "m/s**2"})
    assert units["valid"] is True and "length" in units["dimensionality"]

    inj = execute_tool("evaluate_numeric",
                       {"expr": "__import__('os').getcwd()"})
    assert inj["ok"] is False and "whitelist" in inj["error"]


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))
