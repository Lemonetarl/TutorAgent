
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from scripts.diagnose import Hypothesis

JUDGE_MODEL = "claude-haiku-4-5"
JUDGE_PROMPT_VERSION = "v1"


class JudgeVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    matched_rank: Optional[int] = Field(
        default=None,
        description="1-based rank of the FIRST hypothesis describing the same "
                    "underlying error as the ground-truth cause. Null if none do.")
    reasoning: str = Field(..., description="One sentence: why it matches or doesn't.")


JUDGE_SYSTEM = """\
You judge whether a diagnosis matches a known ground-truth error.

You are given the GROUND-TRUTH cause of a student's wrong answer (the error
was deliberately injected, so this label is authoritative) and a ranked list
of hypotheses from a diagnostic system.

Return matched_rank = the 1-based rank of the FIRST hypothesis that describes
the SAME UNDERLYING ERROR MECHANISM as the ground truth. Same mechanism, not
same wording: "dropped the factor of 2 under the root" matches "forgot the
factor of 2: used sqrt(h/g)". Be strict about mechanism: "made an algebra
mistake" does NOT match "sign error in the exponent" -- vague is not a match.
If no hypothesis matches, matched_rank = null.

Emit exactly one emit_match tool call.
"""


async def judge_match(client, *, injected_cause: str,
                      hypotheses: list[Hypothesis]) -> tuple[JudgeVerdict, int, int]:
    """Returns (verdict, input_tokens, output_tokens)."""
    numbered = "\n".join(
        f"  {i}. {h.misconception} -- {h.explanation}"
        for i, h in enumerate(hypotheses, start=1))
    user = (f"GROUND-TRUTH CAUSE:\n  {injected_cause}\n\n"
            f"HYPOTHESES (ranked):\n{numbered}\n\n"
            f"Which rank, if any, matches the ground truth?")
    msg = await client.create(
        model=JUDGE_MODEL, max_tokens=1000, temperature=0.0,
        system=JUDGE_SYSTEM,
        messages=[{"role": "user", "content": user}],
        tools=[{"name": "emit_match",
                "description": "Emit the match verdict. Exactly once.",
                "input_schema": JudgeVerdict.model_json_schema()}],
        tool_choice={"type": "tool", "name": "emit_match"})
    tin = getattr(msg.usage, "input_tokens", 0)
    tout = getattr(msg.usage, "output_tokens", 0)
    for block in msg.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "emit_match":
            verdict = JudgeVerdict.model_validate(block.input)
            # Defensive clamp: a rank outside the provided list is a judge
            # hallucination and must count as no-match, never as a hit.
            if verdict.matched_rank is not None and not (
                    1 <= verdict.matched_rank <= len(hypotheses)):
                verdict.matched_rank = None
            return verdict, tin, tout
    raise ValueError("judge_match: model did not emit emit_match")
