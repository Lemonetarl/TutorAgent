
from __future__ import annotations

import json
import re
from pathlib import Path

import sympy

from scripts.comparator import _sanitize

_TRAILING = re.compile(r"[.,\s]+$")


def _clean_expr(raw: str) -> str:
    """Strip trailing punctuation/space the bank authors left in prose form."""
    return _TRAILING.sub("", (raw or "").strip())


def _is_parseable(expr: str) -> bool:
    clean = _sanitize(expr)
    if clean is None:
        return False
    try:
        return isinstance(sympy.sympify(clean), sympy.Expr)
    except Exception:  # noqa: BLE001
        return False


def load_and_adapt(path: str, *, verbose: bool = True) -> list[dict]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{path}: expected a JSON list of problems")

    adapted: list[dict] = []
    skipped: list[tuple[str, str]] = []

    for p in raw:
        pid = p.get("id", "<no id>")
        statement = p.get("statement") or p.get("problem_statement")
        if not statement:
            skipped.append((pid, "no statement/problem_statement"))
            continue

        domain = (p.get("domain") or "").strip().lower()
        if domain in ("mathe", "maths"):
            domain = "math"
        if domain not in ("math", "physics"):
            skipped.append((pid, f"unsupported domain {domain!r}"))
            continue

        ca = p.get("correct_answer", {})
        correct_text = _clean_expr(ca.get("text") or ca.get("value") or "")
        if not correct_text:
            skipped.append((pid, "empty correct answer"))
            continue
        if not _is_parseable(correct_text):
            skipped.append((pid, f"correct answer not a parseable scalar/expr "
                                 f"(out of scope): {correct_text!r}"))
            continue

        errors = []
        for e in p.get("injected_errors", []):
            wrong = _clean_expr(e.get("wrong_text") or e.get("sympy_form") or "")
            if not wrong:
                continue
            errors.append({"wrong_text": wrong, "cause": e.get("cause", "unspecified")})

        adapted.append({
            "id": pid,
            "domain": domain,
            "statement": statement,
            "correct_answer": {"text": correct_text},
            "injected_errors": errors,
        })

    if verbose:
        n_trials = sum(1 + len(p["injected_errors"]) for p in adapted)
        print(f"[bank] {len(adapted)} problems adapted ({n_trials} trials); "
              f"{len(skipped)} skipped")
        for pid, why in skipped:
            print(f"[bank]   SKIP {pid}: {why}")
    return adapted


if __name__ == "__main__":
    import sys
    for path in sys.argv[1:] or ["ProblemBank.json"]:
        load_and_adapt(path)
