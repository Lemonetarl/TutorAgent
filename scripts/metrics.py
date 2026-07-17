
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal, Optional

TrialKind = Literal["correct", "wrong"]
Outcome = Literal["verdict", "uncertain", "not_ready", "error"]


@dataclass
class TrialRecord:
    """One pipeline run, flattened. The intermediate representation everything
    downstream (metrics, report) reads."""
    problem_id: str
    domain: str
    trial_kind: TrialKind
    expected_match: bool
    outcome: Outcome
    # populated when outcome == "verdict":
    verdict: Optional[str] = None            # CORRECT/EQUIVALENT/INCORRECT/UNSUPPORTED
    is_match: Optional[bool] = None
    method: str = ""
    detail: str = ""
    # populated on wrong trials:
    injected_cause: Optional[str] = None
    # S5 diagnosis (caught wrong trials, when enabled):
    diagnosis_attempted: bool = False
    diagnosis_top: str = ""                       # top-ranked misconception
    diagnosis_hypotheses: list[str] = field(default_factory=list)
    diagnosis_matched_rank: Optional[int] = None  # judge: 1-based; None = no match
    diagnosis_error: str = ""
    # pipeline context:
    pipeline_status: Optional[str] = None    # HIGH/MEDIUM/UNCERTAIN
    rounds_used: Optional[int] = None
    parse_flags: list[str] = field(default_factory=list)
    # accounting:
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    trace_path: str = ""
    error: str = ""                          # populated when outcome == "error"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class EvalMetrics:
    n_trials: int
    n_correct_trials: int
    n_wrong_trials: int
    n_verdict: int
    n_uncertain: int
    n_not_ready: int
    n_error: int
    catch_rate: Optional[float]
    false_positive_rate: Optional[float]
    uncertain_rate: Optional[float]
    uncertain_rate_correct: Optional[float]
    uncertain_rate_wrong: Optional[float]
    n_diagnosed: int
    diagnosis_top1_accuracy: Optional[float]     # top hypothesis matched cause
    diagnosis_any_accuracy: Optional[float]      # any of the <=3 matched
    total_input_tokens: int
    total_output_tokens: int
    total_seconds: float
    mean_seconds_per_trial: Optional[float]


def _safe_div(num: int, den: int) -> Optional[float]:
    """None, not 0.0, on an empty denominator: 'we have no data' must never
    masquerade as 'we measured zero'."""
    return num / den if den else None


def compute_metrics(records: list[TrialRecord]) -> EvalMetrics:
    correct = [r for r in records if r.trial_kind == "correct"]
    wrong = [r for r in records if r.trial_kind == "wrong"]

    verdicts = [r for r in records if r.outcome == "verdict"]
    uncertain = [r for r in records if r.outcome == "uncertain"]

    # catch rate: wrong trials WITH a verdict, judged not-a-match
    wrong_verdicts = [r for r in wrong if r.outcome == "verdict"]
    caught = [r for r in wrong_verdicts if r.is_match is False]

    # false positives: correct trials WITH a verdict, judged not-a-match
    correct_verdicts = [r for r in correct if r.outcome == "verdict"]
    false_pos = [r for r in correct_verdicts if r.is_match is False]

    # uncertain rates: denominator = trials that entered the pipeline at all
    def _unc_rate(subset: list[TrialRecord]) -> Optional[float]:
        entered = [r for r in subset if r.outcome in ("verdict", "uncertain")]
        return _safe_div(sum(1 for r in entered if r.outcome == "uncertain"),
                         len(entered))

    # diagnosis accuracy: caught wrong trials where S5 ran and did not error.
    # The judge returning None (no hypothesis matched) is a JUDGED MISS and
    # stays in the denominator; a crashed diagnosis does not.
    diagnosed = [r for r in wrong
                 if r.diagnosis_attempted and not r.diagnosis_error]

    total_seconds = sum(r.seconds for r in records)
    return EvalMetrics(
        n_trials=len(records),
        n_correct_trials=len(correct),
        n_wrong_trials=len(wrong),
        n_verdict=len(verdicts),
        n_uncertain=len(uncertain),
        n_not_ready=sum(1 for r in records if r.outcome == "not_ready"),
        n_error=sum(1 for r in records if r.outcome == "error"),
        catch_rate=_safe_div(len(caught), len(wrong_verdicts)),
        false_positive_rate=_safe_div(len(false_pos), len(correct_verdicts)),
        uncertain_rate=_unc_rate(records),
        uncertain_rate_correct=_unc_rate(correct),
        uncertain_rate_wrong=_unc_rate(wrong),
        n_diagnosed=len(diagnosed),
        diagnosis_top1_accuracy=_safe_div(
            sum(1 for r in diagnosed if r.diagnosis_matched_rank == 1),
            len(diagnosed)),
        diagnosis_any_accuracy=_safe_div(
            sum(1 for r in diagnosed if r.diagnosis_matched_rank is not None),
            len(diagnosed)),
        total_input_tokens=sum(r.input_tokens for r in records),
        total_output_tokens=sum(r.output_tokens for r in records),
        total_seconds=total_seconds,
        mean_seconds_per_trial=_safe_div(int(total_seconds * 1000),
                                         len(records)) and total_seconds / len(records),
    )