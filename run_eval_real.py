"""
run_eval_real.py -- drive the eval harness over the REAL problem bank with
REAL Claude API calls, score every trial, and write records + a markdown report.

    python run_eval_real.py                          # ProblemBank.json, full run
    python run_eval_real.py --limit 2                # first 2 problems (cheap smoke)
    python run_eval_real.py MyBank.json --concurrency 5

This spends real tokens. Do a --limit 1 or --limit 2 run first to shake out
prompt friction at a fraction of the cost, THEN run the whole bank.
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from dotenv import load_dotenv

from scripts.bank_loader import load_and_adapt
from scripts.eval_harness import run_eval
from scripts.metrics import compute_metrics
from scripts.report import write_report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("bank", nargs="?", default="ProblemBank.json")
    ap.add_argument("--limit", type=int, default=None,
                    help="run only the first N problems (cheap smoke run)")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--out-dir", default="runs")
    args = ap.parse_args()

    load_dotenv()
    import os
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY not set -- put it in .env")

    # Import AFTER load_dotenv so the SDK sees the key; import here so the
    # module loads even where the SDK isn't installed (offline tests).
    from scripts.solver_pipeline import AnthropicClient, K_SAMPLES, MODEL
    from scripts.parser import PARSER_MODEL

    bank = load_and_adapt(args.bank)
    if args.limit:
        bank = bank[:args.limit]

    n_trials = sum(1 + len(p["injected_errors"]) for p in bank)
    print(f"[run] {len(bank)} problems -> {n_trials} trials, "
          f"concurrency={args.concurrency}. Spending real tokens now...\n")

    client = AnthropicClient()
    records = asyncio.run(run_eval(client, bank, concurrency=args.concurrency))
    metrics = compute_metrics(records)

    config = {"solver_model": MODEL, "parser_model": PARSER_MODEL,
              "k_samples": K_SAMPLES, "concurrency": args.concurrency,
              "banks": [args.bank], "n_problems": len(bank)}
    report_path = write_report(records, metrics, config, out_dir=Path(args.out_dir))

    fmt = lambda x: "n/a" if x is None else f"{x:.0%}"
    print("\n" + "=" * 52)
    print(f"  catch rate          : {fmt(metrics.catch_rate)}")
    print(f"  false-positive rate : {fmt(metrics.false_positive_rate)}")
    print(f"  uncertain rate      : {fmt(metrics.uncertain_rate)}  "
          f"(correct {fmt(metrics.uncertain_rate_correct)} / "
          f"wrong {fmt(metrics.uncertain_rate_wrong)})")
    print(f"  verdicts/uncertain/not-ready/error : "
          f"{metrics.n_verdict}/{metrics.n_uncertain}/"
          f"{metrics.n_not_ready}/{metrics.n_error}")
    print(f"  tokens (in/out)     : {metrics.total_input_tokens} / "
          f"{metrics.total_output_tokens}")
    print(f"  wall time           : {metrics.total_seconds:.1f}s")
    print("=" * 52)
    print(f"\n  report : {report_path}")
    print(f"  records: {report_path.parent}/eval_records_*.jsonl (regenerable, token-free)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
