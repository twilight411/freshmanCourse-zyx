"""Score local Spider 2.0-Lite predictions with its official evaluation suite."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
LITE = ROOT / "spider2-lite"
BEIJING = timezone(timedelta(hours=8))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", choices=["deepseek", "qwen", "both"], default="both")
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    module_path = LITE / "evaluation_suite/evaluate.py"
    spec = importlib.util.spec_from_file_location("spider2_official_evaluate", module_path)
    official = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(official)
    for name in ([args.model] if args.model != "both" else ["deepseek", "qwen"]):
        run = ROOT / "runs" / f"spider2_lite_local_{name}"
        sql_dir = run / "sql"
        ids = [x.stem for x in sql_dir.glob("local*.sql")]
        if not ids:
            raise FileNotFoundError(f"No local SQL files in {sql_dir}")
        params = argparse.Namespace(mode="sql", result_dir=str(sql_dir),
                                    gold_dir=str(LITE / "evaluation_suite/gold"),
                                    max_workers=args.workers, timeout=60)
        with tempfile.TemporaryDirectory(prefix="spider2_eval_") as temp:
            records = official.evaluate_spider2sql(params, Path(temp))
        if len(records) != len(ids):
            raise RuntimeError(f"Official evaluator returned {len(records)} of {len(ids)} results")
        out = run / "scored.jsonl"
        out.write_text("".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in records), encoding="utf-8")
        summary = {"dataset": "Spider 2.0-Lite local SQLite", "scorer": str(module_path),
                   "subset": "local SQLite", "evaluated": len(records),
                   "correct": sum(int(r["score"]) for r in records),
                   "ex_percent": 100 * sum(int(r["score"]) for r in records) / len(records),
                   "generated_beijing": datetime.now(BEIJING).isoformat(timespec="seconds")}
        (run / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{name}: {summary['correct']}/{summary['evaluated']} = {summary['ex_percent']:.2f}%", flush=True)
    tee = sys.stdout
    sys.stdout = original_stdout
    sys.stderr = original_stderr
    tee.close()


if __name__ == "__main__":
    main()
