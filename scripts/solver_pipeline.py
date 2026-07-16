"""
solver_pipeline.py -- Stages 2 (Independent Solver), 3 (Blind Verifier),
and 3b (Adjudicator + Gate) of the answer-checking pipeline.

REAL DEPENDENCIES (already built and tested):
    models.py      -- ProblemOnly, StudentAnswer, ReferenceAnswer, AnswerValue
    comparator.py  -- compare(), expressions_equivalent(); whitelisted sympify

THE ONLY MOCKED DEPENDENCY is the LLM itself: `MockClient` replays scripted
responses through the *same* orchestration code paths that `AnthropicClient`
uses, so concurrency, voting, the tool loop, adjudication retries, and the
review queue are all genuinely exercised offline.

===========================  SYSTEM INVARIANTS  ===========================
I1. The solver NEVER sees the student's answer.
      -> enforce_isolation() raises ValueError before any solver/verifier
         API call. (Explicit raise, not `assert`: `python -O` strips asserts,
         and an invariant that vanishes under an interpreter flag isn't one.)
I2. The verifier NEVER sees the solver's reasoning.
      -> the verifier prompt is built from the problem statement alone, and
         enforce_verifier_blindness() trips if any solver step equation leaks
         into it. On retry rounds, ONLY the adjudicator's contradiction
         sentence crosses over -- evidence, not reasoning chains. (The
         adjudicator is the single component permitted to see both sides.)
I3. UNCERTAIN is a first-class output.
      -> if solver and verifier haven't converged within MAX_ROUNDS, the
         result is status=UNCERTAIN, blocked=True, reference=None, and the
         full trace is appended to the human review queue. Nothing downstream
         may issue a verdict from a blocked result.

CONVERGENCE STATUS SEMANTICS
    HIGH      unanimous k solver samples AND verifier agreement, first round
    MEDIUM    majority (non-unanimous) agreement, or convergence on round >=2
    UNCERTAIN no solver<->verifier agreement within MAX_ROUNDS
"""
from __future__ import annotations

import asyncio
import json
import multiprocessing as mp
import random
import re
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from scripts.comparator import compare, expressions_equivalent
from scripts.models import AnswerType, AnswerValue, ProblemOnly, ReferenceAnswer, StudentAnswer

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
MODEL = "claude-sonnet-5"          # solver / verifier / adjudicator routing
K_SAMPLES = 3                      # self-consistency width (3..5)
SOLVER_TEMPERATURE = 0.8           # variation for self-consistency
DETERMINISTIC_TEMPERATURE = 0.0    # adjudicator
MAX_ROUNDS = 3                     # initial solve + up to 2 evidence retries
MAX_TOOL_ITERATIONS = 8            # verifier tool-loop ceiling
SUBPROCESS_TIMEOUT_S = 5.0         # SymPy guard
RUNS_DIR = Path("runs")
# Spec said `.json`; JSONL is used deliberately: appending to a JSON array is
# read-modify-write and corrupts on crash, while JSONL appends are atomic.
REVIEW_QUEUE_PATH = Path("human_review_queue.jsonl")

PROMPT_VERSIONS = {"solver": "v1", "verifier": "v1", "adjudicator": "v1"}

SOLVER_SYSTEM = """\
You are a meticulous physics and mathematics solver (first-year engineering
level: Newtonian kinematics, first/second order ODEs).
Rules:
- Work in explicitly numbered steps. Each step states the action, the
  governing equation, and the result of that step.
- Prefer exact symbolic form (fractions, sqrt) over decimals until the final
  numeric evaluation.
- Every expression you emit must be SymPy-parseable: explicit '*', '**' for
  powers, sqrt()/sin()/cos()/exp(); NO '=' signs inside expressions; NO units
  inside expressions (units go in the dedicated unit field).
- answer_type describes the answer you actually produced, not a guess.
When finished, call the emit_solution tool exactly once."""

VERIFIER_SYSTEM = """\
You are an independent verifier. You have NOT seen any other solution to this
problem and you must not assume one exists.
Rules:
- Solve the problem yourself using a genuinely DIFFERENT method than the most
  standard textbook route (e.g. energy methods instead of kinematics;
  Laplace/undetermined-coefficients instead of characteristic equation).
- Before submitting, use the deterministic tools to check your own work:
  numeric evaluation of your result, symbolic equivalence of intermediate
  forms, and dimensional validity of your units.
- Expressions must be SymPy-parseable; no '=' signs; units only in the unit
  field. answer_type describes what you actually produced.
When finished, call the submit_solution tool exactly once."""

ADJUDICATOR_SYSTEM = """\
Two solutions to the same problem, produced independently, disagree. You are
the only component permitted to read both. Trace the two step logs and locate
the FIRST step at which they mathematically contradict each other.
Output the contradiction as a specific, self-contained mathematical statement
(quantities, equations, dimensions) -- evidence a fresh solver can act on
without seeing either reasoning chain. Do not narrate; do not take sides
beyond the suspected_faulty field. Call emit_adjudication exactly once."""

# ---------------------------------------------------------------------------
# Schemas (Pydantic v2). FinalAnswer extends the spec with `answer_type`
# (the solver's self-description -- established design) and
# `additional_values` (so root sets are representable).
# ---------------------------------------------------------------------------


class SolutionStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int = Field(..., description="1-based step number.")
    action: str = Field(..., description="What this step does, one clause.")
    equation: str = Field(..., description="Governing relation, SymPy syntax.")
    result: str = Field(..., description="This step's outcome, SymPy syntax.")


class FinalAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = Field(..., description="Final value, SymPy-parseable. Numeric string for scalars.")
    unit: Optional[str] = Field(None, description="Units, e.g. 'm/s'. Null if dimensionless.")
    symbolic_form: str = Field(..., description="Exact closed form, e.g. 'sqrt(2*h/g)'.")
    answer_type: AnswerType = Field(..., description="Shape of the answer you produced.")
    additional_values: list[str] = Field(
        default_factory=list,
        description="Only for answer_type='root_set': the remaining roots beyond `value`.")


class StructuredSolution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    given: list[str] = Field(..., description="Extracted knowns, e.g. 'h = 20 m'.")
    unknowns: list[str] = Field(..., description="Target quantities.")
    method_name: str = Field(..., description="Short name of the method used.")
    steps: list[SolutionStep]
    final_answer: FinalAnswer


class Adjudication(BaseModel):
    model_config = ConfigDict(extra="forbid")
    first_divergent_solver_step: Optional[int]
    first_divergent_verifier_step: Optional[int]
    contradiction: str = Field(
        ..., description="The specific mathematical contradiction, self-contained.")
    suspected_faulty: Literal["solver", "verifier", "both", "unclear"]


ConvergenceStatus = Literal["HIGH", "MEDIUM", "UNCERTAIN"]


class PipelineResult(BaseModel):
    status: ConvergenceStatus
    blocked: bool                      # True <=> UNCERTAIN: no verdict may be issued
    reference: Optional[ReferenceAnswer]
    rounds_used: int
    solver_votes: int                  # winning-cluster size in the final round
    solver_k: int
    verifier_method: Optional[str]
    trace_path: str


# ---------------------------------------------------------------------------
# Invariant tripwires
# ---------------------------------------------------------------------------

def enforce_isolation(prompt_text: str, quarantine: Optional[StudentAnswer]) -> None:
    """I1. Called before EVERY solver/verifier call. The quarantined answer is
    used here for one purpose only: proving it is absent from the prompt."""
    if quarantine is None:
        return
    needles = [quarantine.answer_text.strip()]
    needles += [v.sympy_form.strip() for v in quarantine.values]
    for n in needles:
        # Token-boundary match: '2.0' must not fire inside '2.02' or '12.05'.
        if len(n) > 1 and re.search(rf"(?<![\w.]){re.escape(n)}(?![\w.])",
                                    prompt_text):
            raise ValueError(
                f"ISOLATION BREACH (I1): student-answer fragment {n!r} found in a "
                f"solver-bound prompt. Refusing to call the model.")


def enforce_verifier_blindness(prompt_text: str,
                               solver_solutions: list[StructuredSolution]) -> None:
    """I2. The verifier prompt must contain no solver reasoning."""
    for sol in solver_solutions:
        for st in sol.steps:
            frag = st.equation.strip()
            if len(frag) > 3 and frag in prompt_text:
                raise ValueError(
                    f"BLINDNESS BREACH (I2): solver step equation {frag!r} found "
                    f"in the verifier prompt. Refusing to call the model.")


# ---------------------------------------------------------------------------
# Deterministic SymPy tools (fixed, parameterized -- never exec/eval of
# model-generated code). Heavy calls run in a killable subprocess: all task
# functions are module-level because Windows 'spawn' pickles them.
# ---------------------------------------------------------------------------
_SAFE_EXPR = re.compile(r"^[0-9a-zA-Z_+\-*/^(). ,]*$")


def _sanitize(s: str) -> Optional[str]:
    s = s.strip()
    if not s or "__" in s or not _SAFE_EXPR.match(s):
        return None
    return s


def _task_evalf(expr: str, subs: dict[str, float]) -> float:
    from sympy import Symbol, sympify
    e = sympify(expr)
    return float(e.evalf(subs={Symbol(k): float(v) for k, v in subs.items()}))


_SUBPROC_TASKS = {"evalf": _task_evalf}


def _subproc_runner(q: "mp.Queue", name: str, args: tuple) -> None:
    try:
        q.put(("ok", _SUBPROC_TASKS[name](*args)))
    except Exception as e:  # noqa: BLE001 -- any failure is a tool error string
        q.put(("err", f"{type(e).__name__}: {e}"))


def run_guarded(name: str, args: tuple,
                seconds: float = SUBPROCESS_TIMEOUT_S) -> tuple[str, Any]:
    ctx = mp.get_context("spawn")
    q: "mp.Queue" = ctx.Queue()
    p = ctx.Process(target=_subproc_runner, args=(q, name, args), daemon=True)
    p.start()
    p.join(seconds)
    if p.is_alive():
        p.terminate()
        p.join(1.0)
        return ("timeout", None)
    return q.get() if not q.empty() else ("err", "no result")


try:
    import pint
    _UREG = pint.UnitRegistry()
except ImportError:
    _UREG = None


def tool_check_symbolic_equivalence(expr_a: str, expr_b: str) -> dict:
    r = expressions_equivalent(expr_a, expr_b)   # whitelisted + guarded inside
    verdict_map = {"EQUIVALENT": True, "CORRECT": True, "INCORRECT": False}
    return {"equivalent": verdict_map.get(r.verdict),  # None = undetermined
            "method": r.method, "detail": r.detail}


def tool_evaluate_numeric(expr: str, substitutions: Optional[dict] = None) -> dict:
    clean = _sanitize(expr)
    if clean is None:
        return {"ok": False, "error": "expression failed the character whitelist"}
    subs = substitutions or {}
    if not all(isinstance(v, (int, float)) for v in subs.values()):
        return {"ok": False, "error": "substitutions must map symbol -> number"}
    status, val = run_guarded("evalf", (clean, subs))
    if status == "ok":
        return {"ok": True, "value": val}
    return {"ok": False, "error": f"{status}: {val}"}


def tool_check_units(unit_str: str) -> dict:
    if _UREG is None:
        return {"valid": None, "error": "pint unavailable"}
    try:
        u = _UREG(unit_str)
        return {"valid": True, "dimensionality": str(u.dimensionality)}
    except Exception as e:  # noqa: BLE001
        return {"valid": False, "error": str(e)[:120]}


SYMPY_TOOLS: list[dict] = [
    {"name": "check_symbolic_equivalence",
     "description": "Deterministically test whether two SymPy expressions are "
                    "mathematically equivalent. Returns equivalent true/false, "
                    "or null when undetermined.",
     "input_schema": {"type": "object",
                      "properties": {"expr_a": {"type": "string"},
                                     "expr_b": {"type": "string"}},
                      "required": ["expr_a", "expr_b"]}},
    {"name": "evaluate_numeric",
     "description": "Numerically evaluate a SymPy expression with numeric "
                    "substitutions for its symbols. Timeout-guarded.",
     "input_schema": {"type": "object",
                      "properties": {"expr": {"type": "string"},
                                     "substitutions": {
                                         "type": "object",
                                         "additionalProperties": {"type": "number"}}},
                      "required": ["expr"]}},
    {"name": "check_units",
     "description": "Validate a unit string with pint and report its dimensionality.",
     "input_schema": {"type": "object",
                      "properties": {"unit_str": {"type": "string"}},
                      "required": ["unit_str"]}},
]

_TOOL_DISPATCH = {
    "check_symbolic_equivalence": lambda a: tool_check_symbolic_equivalence(**a),
    "evaluate_numeric": lambda a: tool_evaluate_numeric(**a),
    "check_units": lambda a: tool_check_units(**a),
}


def execute_tool(name: str, args: dict) -> dict:
    fn = _TOOL_DISPATCH.get(name)
    if fn is None:
        return {"error": f"unknown tool {name!r}"}
    try:
        return fn(args)
    except TypeError as e:
        return {"error": f"bad arguments for {name}: {e}"}


# ---------------------------------------------------------------------------
# LLM clients. Both expose  async create(**kwargs) -> message  with
# .content (blocks having .type/.name/.input/.id), .stop_reason, .usage --
# the anthropic SDK shape. The orchestration code is identical for both.
# ---------------------------------------------------------------------------

@dataclass
class FakeUsage:
    input_tokens: int = 350
    output_tokens: int = 220


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict
    type: str = "tool_use"


@dataclass
class FakeMessage:
    content: list
    stop_reason: str = "tool_use"
    usage: FakeUsage = field(default_factory=FakeUsage)


class MockClient:
    """Replays a scripted list of messages, FIFO. The script models the LLM;
    everything else in the pipeline runs for real."""

    def __init__(self, script: list[FakeMessage]):
        self._script = list(script)
        self.calls: list[dict] = []

    async def create(self, **kwargs) -> FakeMessage:
        self.calls.append(kwargs)
        if not self._script:
            raise AssertionError("MockClient script exhausted -- the pipeline "
                                 "made more calls than the scenario scripted")
        return self._script.pop(0)


async def _with_backoff(coro_factory, retries: int = 5):
    """Exponential backoff + jitter for 429/500/529. Anything else re-raises."""
    delay = 1.0
    last: Optional[Exception] = None
    for _ in range(retries):
        try:
            return await coro_factory()
        except Exception as e:  # noqa: BLE001
            status = getattr(e, "status_code", None)
            retryable = status in (429, 500, 529) or type(e).__name__ in (
                "RateLimitError", "InternalServerError", "OverloadedError",
                "APIConnectionError", "APITimeoutError")
            if not retryable:
                raise
            last = e
            await asyncio.sleep(delay + random.random() * 0.25 * delay)
            delay = min(delay * 2, 30.0)
    raise RuntimeError(f"retries exhausted; last error: {last!r}")


class AnthropicClient:
    """Thin wrapper over the real SDK with backoff. Constructed lazily so the
    module imports (and the mock tests run) without the SDK installed."""

    # Sonnet 5, the Opus 4.x line, and Fable/Mythos 5 sample adaptively and
    # return a 400 if temperature/top_p/top_k are set to ANY value (including 0).
    # Haiku 4.5 still accepts them. Rather than make every caller know this, the
    # wrapper strips sampling params for the models that reject them.
    _NO_SAMPLING_PREFIXES = ("claude-sonnet-5", "claude-opus-4", "claude-fable", "claude-mythos")

    def __init__(self) -> None:
        from anthropic import AsyncAnthropic  # requires ANTHROPIC_API_KEY
        self._client = AsyncAnthropic()

    @classmethod
    def _clean(cls, kwargs: dict) -> dict:
        model = kwargs.get("model", "")
        if any(model.startswith(p) for p in cls._NO_SAMPLING_PREFIXES):
            for p in ("temperature", "top_p", "top_k"):
                kwargs.pop(p, None)
        return kwargs

    async def create(self, **kwargs):
        kwargs = self._clean(kwargs)
        return await _with_backoff(lambda: self._client.messages.create(**kwargs))


# ---------------------------------------------------------------------------
# Trace: one JSON file per problem, plus the token/latency meter.
# ---------------------------------------------------------------------------

class RunTrace:
    def __init__(self, problem: ProblemOnly):
        self.problem_id = uuid.uuid4().hex[:10]
        self.problem = problem.statement
        self.domain = problem.subject_domain
        self.t0 = time.time()
        self.events: list[dict] = []
        self.stage_tokens: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        self.stage_seconds: dict[str, float] = defaultdict(float)
        self.status: Optional[str] = None

    def tick(self, stage: str, usage: Any, seconds: float) -> None:
        self.stage_tokens[stage][0] += getattr(usage, "input_tokens", 0)
        self.stage_tokens[stage][1] += getattr(usage, "output_tokens", 0)
        self.stage_seconds[stage] += seconds

    def event(self, stage: str, kind: str, payload: Any) -> None:
        self.events.append({"t": round(time.time() - self.t0, 3),
                            "stage": stage, "kind": kind, "payload": payload})

    def to_dict(self) -> dict:
        return {"problem_id": self.problem_id, "problem": self.problem,
                "domain": self.domain, "status": self.status,
                "prompt_versions": PROMPT_VERSIONS,
                "tokens_by_stage": {k: {"in": v[0], "out": v[1]}
                                    for k, v in self.stage_tokens.items()},
                "seconds_by_stage": {k: round(v, 3)
                                     for k, v in self.stage_seconds.items()},
                "events": self.events}

    def save(self) -> Path:
        RUNS_DIR.mkdir(exist_ok=True)
        path = RUNS_DIR / f"trace_{self.problem_id}.json"
        path.write_text(json.dumps(self.to_dict(), indent=2))
        return path


def append_review_queue(trace: RunTrace, reason: str) -> None:
    row = {"queued_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "problem_id": trace.problem_id, "reason": reason,
           "trace": trace.to_dict()}
    with REVIEW_QUEUE_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


# ---------------------------------------------------------------------------
# Structured-output helper: a forced tool call whose input_schema is the
# Pydantic model's JSON schema. This guarantees schema-conformant output AND
# composes with the SymPy tools in the same request. (If you prefer the
# native structured-outputs API surface, swap it in here -- check
# platform.claude.com/docs for the current parameter; the guarantee is the
# same, this route is SDK-version-safe.)
# ---------------------------------------------------------------------------

def _schema_tool(model_cls: type[BaseModel], name: str, description: str) -> dict:
    return {"name": name, "description": description,
            "input_schema": model_cls.model_json_schema()}

TModel = TypeVar("TModel", bound=BaseModel)

async def call_structured(client, *, system: str, user_content: str,
                          schema_model: type[TModel], tool_name: str,
                          temperature: float, trace: RunTrace, stage: str):
    t0 = time.perf_counter()
    msg = await client.create(
        model=MODEL, max_tokens=3000, temperature=temperature, system=system,
        messages=[{"role": "user", "content": user_content}],
        tools=[_schema_tool(schema_model, tool_name,
                            f"Emit the final {schema_model.__name__} object.")],
        tool_choice={"type": "tool", "name": tool_name})
    trace.tick(stage, msg.usage, time.perf_counter() - t0)
    for block in msg.content:
        if getattr(block, "type", None) == "tool_use" and block.name == tool_name:
            return schema_model.model_validate(block.input)
    raise ValueError(f"{stage}: model did not emit {tool_name}")


async def run_tool_loop(client, *, system: str, user_content: str,
                        submit_model: type[TModel], submit_tool: str,
                        temperature: float, trace: RunTrace, stage: str):
    """Deterministic tool-use loop with an explicit iteration ceiling."""
    messages: list[dict] = [{"role": "user", "content": user_content}]
    tools = SYMPY_TOOLS + [_schema_tool(
        submit_model, submit_tool,
        "Submit your final structured solution. Call exactly once, when done.")]
    for _ in range(MAX_TOOL_ITERATIONS):
        t0 = time.perf_counter()
        msg = await client.create(model=MODEL, max_tokens=3000,
                                  temperature=temperature, system=system,
                                  messages=messages, tools=tools)
        trace.tick(stage, msg.usage, time.perf_counter() - t0)
        if msg.stop_reason != "tool_use":
            raise ValueError(f"{stage}: expected tool_use, got {msg.stop_reason!r}")
        tool_results: list[dict] = []
        submitted = None
        for b in msg.content:
            if getattr(b, "type", None) != "tool_use":
                continue
            if b.name == submit_tool:
                submitted = submit_model.model_validate(b.input)
                tool_results.append({"type": "tool_result",
                                     "tool_use_id": b.id, "content": "received"})
            else:
                out = execute_tool(b.name, b.input)
                trace.event(stage, "tool_call",
                            {"name": b.name, "input": b.input, "output": out})
                tool_results.append({"type": "tool_result", "tool_use_id": b.id,
                                     "content": json.dumps(out)})
        if submitted is not None:
            return submitted
        messages.append({"role": "assistant", "content": msg.content})
        messages.append({"role": "user", "content": tool_results})
    raise TimeoutError(f"{stage}: tool loop exceeded {MAX_TOOL_ITERATIONS} iterations")


# ---------------------------------------------------------------------------
# Stage 2 -- Independent Solver (k concurrent samples + comparator vote)
# ---------------------------------------------------------------------------

_EVIDENCE_BLOCK = ("\n\nINDEPENDENT CHECK EVIDENCE -- a prior attempt at this "
                   "problem surfaced the following mathematical contradiction. "
                   "Solve from scratch; trust the mathematics, not any prior "
                   "answer:\n{evidence}")


def build_solver_user(problem: ProblemOnly, evidence: Optional[str]) -> str:
    text = (f"Domain: {problem.subject_domain}\n\nPROBLEM:\n{problem.statement}\n\n"
            "Produce your structured solution now.")
    if evidence:
        text += _EVIDENCE_BLOCK.format(evidence=evidence)
    return text


async def _solve_once(client, problem: ProblemOnly,
                      quarantine: Optional[StudentAnswer],
                      evidence: Optional[str], trace: RunTrace) -> StructuredSolution:
    # I1 is checked on the PRE-EVIDENCE prompt. Adjudicator evidence is exempt
    # by PROVENANCE, not by content: the adjudicator never receives student
    # data, so evidence cannot carry it -- except by numeric coincidence, and
    # the coincidence case is exactly a student whose answer is (nearly)
    # right. Content-scanning evidence would brick the retry loop then.
    enforce_isolation(SOLVER_SYSTEM + "\n" + build_solver_user(problem, None),
                      quarantine)                                       # I1
    user = build_solver_user(problem, evidence)
    return await call_structured(client, system=SOLVER_SYSTEM, user_content=user,
                                 schema_model=StructuredSolution,
                                 tool_name="emit_solution",
                                 temperature=SOLVER_TEMPERATURE,
                                 trace=trace, stage="solver")


def _same_answer(a: FinalAnswer, b: FinalAnswer, domain: str) -> bool:
    """The comparator IS the majority-vote primitive. Disagreement on
    answer_type counts as disagreement -- it is itself a divergence signal."""
    if a.answer_type != b.answer_type:
        return False
    pick = (lambda f: f.symbolic_form) if a.answer_type == "symbolic" \
        else (lambda f: f.value)
    return compare([pick(a)], a.unit, [pick(b)], b.unit,
                   a.answer_type, domain).is_match


def majority_vote(samples: list[StructuredSolution],
                  domain: str) -> tuple[StructuredSolution, int]:
    clusters: list[list[StructuredSolution]] = []
    for s in samples:
        for c in clusters:
            if _same_answer(s.final_answer, c[0].final_answer, domain):
                c.append(s)
                break
        else:
            clusters.append([s])
    clusters.sort(key=len, reverse=True)
    return clusters[0][0], len(clusters[0])


async def solve_stage(client, problem: ProblemOnly, *, k: int,
                      quarantine: Optional[StudentAnswer],
                      evidence: Optional[str],
                      trace: RunTrace) -> tuple[StructuredSolution, int]:
    samples = await asyncio.gather(*[
        _solve_once(client, problem, quarantine, evidence, trace)
        for _ in range(k)])
    winner, votes = majority_vote(list(samples), problem.subject_domain)
    trace.event("solver", "vote",
                {"k": k, "winning_votes": votes,
                 "answers": [s.final_answer.model_dump() for s in samples]})
    return winner, votes


# ---------------------------------------------------------------------------
# Stage 3 -- Blind Verifier (fresh context, different method, fixed tools)
# ---------------------------------------------------------------------------

def build_verifier_user(problem: ProblemOnly, evidence: Optional[str]) -> str:
    text = (f"Domain: {problem.subject_domain}\n\nPROBLEM:\n{problem.statement}\n\n"
            "Solve independently with a different method, verify with the tools, "
            "then submit.")
    if evidence:
        text += _EVIDENCE_BLOCK.format(evidence=evidence)
    return text


async def verify_stage(client, problem: ProblemOnly, *,
                       quarantine: Optional[StudentAnswer],
                       solver_samples: list[StructuredSolution],
                       evidence: Optional[str],
                       trace: RunTrace) -> StructuredSolution:
    base = VERIFIER_SYSTEM + "\n" + build_verifier_user(problem, None)
    enforce_isolation(base, quarantine)                                  # I1
    enforce_verifier_blindness(base, solver_samples)                     # I2
    user = build_verifier_user(problem, evidence)  # evidence exempt by provenance
    sol = await run_tool_loop(client, system=VERIFIER_SYSTEM, user_content=user,
                              submit_model=StructuredSolution,
                              submit_tool="submit_solution",
                              temperature=DETERMINISTIC_TEMPERATURE,
                              trace=trace, stage="verifier")
    trace.event("verifier", "solution", sol.final_answer.model_dump())
    return sol


# ---------------------------------------------------------------------------
# Stage 3b -- Adjudicator + Gate
# ---------------------------------------------------------------------------

async def adjudicate_stage(client, problem: ProblemOnly,
                           solver_sol: StructuredSolution,
                           verifier_sol: StructuredSolution,
                           trace: RunTrace) -> Adjudication:
    payload = json.dumps({
        "problem": problem.statement,
        "solution_A_solver": solver_sol.model_dump(),
        "solution_B_verifier": verifier_sol.model_dump()}, indent=2)
    adj = await call_structured(
        client, system=ADJUDICATOR_SYSTEM,
        user_content=f"Locate the first mathematical contradiction:\n{payload}",
        schema_model=Adjudication, tool_name="emit_adjudication",
        temperature=DETERMINISTIC_TEMPERATURE, trace=trace, stage="adjudicator")
    trace.event("adjudicator", "adjudication", adj.model_dump())
    return adj


def to_reference(fa: FinalAnswer) -> ReferenceAnswer:
    primary = fa.symbolic_form if fa.answer_type == "symbolic" else fa.value
    values = [AnswerValue(sympy_form=primary)]
    values += [AnswerValue(sympy_form=v) for v in fa.additional_values]
    return ReferenceAnswer(answer_type=fa.answer_type, values=values, units=fa.unit)


async def run_pipeline(client, problem: ProblemOnly, *,
                       quarantine: Optional[StudentAnswer] = None,
                       k: int = K_SAMPLES,
                       max_rounds: int = MAX_ROUNDS) -> PipelineResult:
    """The gate. Returns a ReferenceAnswer only on convergence; otherwise a
    blocked UNCERTAIN result routed to the human review queue."""
    trace = RunTrace(problem)
    evidence: Optional[str] = None
    votes = 0
    verifier_sol: Optional[StructuredSolution] = None

    for rnd in range(1, max_rounds + 1):
        winner, votes = await solve_stage(client, problem, k=k,
                                          quarantine=quarantine,
                                          evidence=evidence, trace=trace)
        verifier_sol = await verify_stage(client, problem,
                                          quarantine=quarantine,
                                          solver_samples=[winner],
                                          evidence=evidence, trace=trace)
        matched = _same_answer(winner.final_answer, verifier_sol.final_answer,
                               problem.subject_domain)
        trace.event("gate", "round",
                    {"round": rnd, "solver_votes": votes, "k": k,
                     "matched": matched})
        if matched:
            trace.status = "HIGH" if (votes == k and rnd == 1) else "MEDIUM"
            return PipelineResult(
                status=trace.status, blocked=False,
                reference=to_reference(winner.final_answer),
                rounds_used=rnd, solver_votes=votes, solver_k=k,
                verifier_method=verifier_sol.method_name,
                trace_path=str(trace.save()))

        # Divergence: adjudicate. The contradiction (evidence, not reasoning)
        # seeds the next round -- if one remains.
        adj = await adjudicate_stage(client, problem, winner, verifier_sol, trace)
        evidence = adj.contradiction

    trace.status = "UNCERTAIN"
    path = trace.save()
    append_review_queue(trace, reason="no solver<->verifier convergence "
                                      f"within {max_rounds} rounds")
    return PipelineResult(status="UNCERTAIN", blocked=True, reference=None,
                          rounds_used=max_rounds, solver_votes=votes,
                          solver_k=k,
                          verifier_method=(verifier_sol.method_name
                                           if verifier_sol else None),
                          trace_path=str(path))