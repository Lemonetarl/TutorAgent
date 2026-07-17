
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from pydantic import ValidationError

from scripts.models import ParsedInput
from scripts.prompts import (PARSER_PROMPT_VERSION, PARSER_SYSTEM,
                             RETRY_TEMPLATE, build_parser_user)
from scripts.validators import apply_findings, run_validators

PARSER_MODEL = "claude-haiku-4-5"
PARSER_TEMPERATURE = 0.0
MAX_ATTEMPTS = 2          # first try + one error-feedback retry
MAX_TOKENS = 2000


class ParserError(RuntimeError):
    """Extraction failed after all attempts. The orchestrator's move is to ask
    the person to restate the submission -- never to guess a parse."""


# ---------------------------------------------------------------------------
# Layer 1: deterministic marker split. Cheap, dumb on purpose.
# ---------------------------------------------------------------------------
_MARKER = re.compile(
    r"(?is)^\s*(?:problem|question|q)\s*[:\-]\s*(?P<problem>.+?)"
    r"(?:\n|\s)\s*(?:my\s+answer|student\s+answer|answer|a)\s*[:\-]\s*"
    r"(?P<answer>.+)\s*$")
_PROBLEM_ONLY_MARKER = re.compile(
    r"(?is)^\s*(?:problem|question|q)\s*[:\-]\s*(?P<problem>.+)\s*$")


def split_fast_path(raw_text: str) -> Optional[tuple[str, Optional[str]]]:
    """Returns (problem_half, answer_half_or_None) when explicit markers pin
    the boundary; None when they don't. Never guesses."""
    m = _MARKER.match(raw_text)
    if m:
        return m.group("problem"), m.group("answer")
    m = _PROBLEM_ONLY_MARKER.match(raw_text)
    if m:
        return m.group("problem"), None
    return None


# ---------------------------------------------------------------------------
# Trace: same tick() shape as solver_pipeline.RunTrace so cost/latency
# accounting aggregates uniformly, without importing S2's machinery.
# ---------------------------------------------------------------------------
@dataclass
class ParserTrace:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    events: list[dict] = field(default_factory=list)

    def tick(self, stage: str, usage: Any, seconds: float) -> None:
        self.calls += 1
        self.input_tokens += getattr(usage, "input_tokens", 0)
        self.output_tokens += getattr(usage, "output_tokens", 0)
        self.seconds += seconds

    def event(self, stage: str, kind: str, payload: Any) -> None:
        self.events.append({"stage": stage, "kind": kind, "payload": payload})


@dataclass
class ParseOutcome:
    parsed: ParsedInput
    attempts: int
    fast_path_used: bool
    prompt_version: str
    trace: ParserTrace


# ---------------------------------------------------------------------------
# Layer 2: the one Haiku call, schema-forced.
# ---------------------------------------------------------------------------
async def _call_haiku_structured(client, user_content: str,
                                 trace: ParserTrace) -> ParsedInput:
    t0 = time.perf_counter()
    msg = await client.create(
        model=PARSER_MODEL, max_tokens=MAX_TOKENS,
        temperature=PARSER_TEMPERATURE, system=PARSER_SYSTEM,
        messages=[{"role": "user", "content": user_content}],
        tools=[{"name": "emit_parse",
                "description": "Emit the final ParsedInput object. Exactly once.",
                "input_schema": ParsedInput.model_json_schema()}],
        tool_choice={"type": "tool", "name": "emit_parse"})
    trace.tick("parser", msg.usage, time.perf_counter() - t0)
    for block in msg.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "emit_parse":
            return ParsedInput.model_validate(block.input)   # may raise ValidationError
    raise ParserError("model did not emit the emit_parse tool call")


# ---------------------------------------------------------------------------
# The entry point.
# ---------------------------------------------------------------------------
async def parse(client, raw_text: str) -> ParseOutcome:
    if not raw_text or not raw_text.strip():
        raise ParserError("empty submission -- nothing to parse")

    trace = ParserTrace()
    presplit = split_fast_path(raw_text)
    base_user = build_parser_user(raw_text, presplit)

    parsed: Optional[ParsedInput] = None
    last_error: Optional[str] = None
    attempts = 0
    for attempts in range(1, MAX_ATTEMPTS + 1):
        user = base_user if last_error is None else (
            base_user + RETRY_TEMPLATE.format(error=last_error))
        try:
            parsed = await _call_haiku_structured(client, user, trace)
            break
        except ValidationError as e:
            # The error-feedback loop: hand the exact failure back and retry.
            last_error = str(e)[:1500]
            trace.event("parser", "validation_retry", {"error": last_error})
    if parsed is None:
        raise ParserError(
            f"extraction failed validation after {MAX_ATTEMPTS} attempts; "
            f"last error: {last_error}")

    findings = run_validators(raw_text, parsed)
    if findings:
        trace.event("parser", "layer3_findings",
                    [f"{f.flag}: {f.note}" for f in findings])
    parsed = apply_findings(parsed, findings)

    return ParseOutcome(parsed=parsed, attempts=attempts,
                        fast_path_used=presplit is not None,
                        prompt_version=PARSER_PROMPT_VERSION, trace=trace)


def parse_sync(client, raw_text: str) -> ParseOutcome:
    """Convenience wrapper for Gradio / scripts."""
    return asyncio.run(parse(client, raw_text))