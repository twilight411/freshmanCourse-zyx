"""Score local Spider 2.0-Lite SQL using the official CSV comparator and a real SQL timeout."""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parent
LITE = ROOT / "spider2-lite"
BEIJING = timezone(timedelta(hours=8))


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def execute_to_csv(instance_id: str, db: str, sql: str, target: Path, timeout: int) -> dict:
    if not re.match(r"^\s*(?:--[^\n]*\n\s*)*(SELECT|WITH)\b", sql or "", re.I):
        return {"error": "SQL must start with SELECT or WITH"}
    source = LITE / "resource/databases/spider2-localdb" / f"{db}.sqlite"
    uri = source.resolve().as_uri() + "?mode=ro"
    start = time.monotonic()
    temp = target.with_suffix(".tmp")
    if temp.exists():
        temp.unlink()
    conn = sqlite3.connect(uri, uri=True)
    conn.set_progress_handler(lambda: int(time.monotonic() - start > timeout), 1000)
    try:
        wrote = False
        for chunk in pd.read_sql_query(sql, conn, chunksize=500):
            chunk.to_csv(temp, index=False, header=not wrote, mode="a" if wrote else "w")
            wrote = True
        if not wrote:
            pd.read_sql_query(sql, conn).to_csv(temp, index=False)
        temp.replace(target)
        return {"elapsed_seconds": round(time.monotonic() - start, 3)}
    except BaseException as exc:
        if temp.exists():
            temp.unlink()
        return {"error": f"{type(exc).__name__}: {str(exc)[:500]}",
                "timeout": time.monotonic() - start >= timeout}
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", choices=["deepseek", "qwen", "deepseek_whu_nonthinking", "both"], default="both")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()
    original_stdout, original_stderr = sys.stdout, sys.stderr
    spec = importlib.util.spec_from_file_location("spider2_official_evaluate", LITE / "evaluation_suite/evaluate.py")
    official = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(official)
    examples = {r["instance_id"]: r for r in load_jsonl(LITE / "spider2-lite.jsonl") if r["instance_id"].startswith("local")}
    standards = official.load_jsonl_to_dict(str(LITE / "evaluation_suite/gold/spider2lite_eval.jsonl"))
    for name in ([args.model] if args.model != "both" else ["deepseek", "qwen"]):
        run = ROOT / "runs" / f"spider2_lite_local_{name}"
        csv_dir = run / "sql_csv"
        csv_dir.mkdir(exist_ok=True)
        pred = {r["instance_id"]: r for r in load_jsonl(run / "predictions.jsonl")}
        if len(pred) != 135:
            raise RuntimeError(f"{name}: only {len(pred)}/135 model predictions")
        error_file = run / "execution_errors.jsonl"
        errors = {r["instance_id"]: r for r in load_jsonl(error_file)} if error_file.exists() else {}
        pending = [(id, x) for id, x in examples.items() if not (csv_dir / f"{id}.csv").exists() and id not in errors]
        print(f"[{datetime.now(BEIJING).isoformat(timespec='seconds')}] {name}: "
              f"{len(list(csv_dir.glob('local*.csv')))} output CSVs reused; {len(pending)} SQLs pending", flush=True)
        with error_file.open("a", encoding="utf-8") as f, ThreadPoolExecutor(max_workers=args.workers) as pool:
            future_map = {pool.submit(execute_to_csv, id, x["db"], pred[id].get("predicted_sql", ""),
                                      csv_dir / f"{id}.csv", args.timeout): id for id, x in pending}
            for future in as_completed(future_map):
                id = future_map[future]
                result = future.result()
                if "error" in result:
                    entry = {"instance_id": id, **result}
                    errors[id] = entry
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                    f.flush()
                print(f"{name} {id}: {'saved' if 'error' not in result else result['error'][:120]}", flush=True)
        scored = []
        gold_csv_dir = LITE / "evaluation_suite/gold/exec_result"
        for id in sorted(examples):
            if (csv_dir / f"{id}.csv").exists():
                record = official.evaluate_single_exec_result_instance(id, standards, str(csv_dir), str(gold_csv_dir))
            else:
                record = {"instance_id": id, "score": 0,
                          "error_info": errors.get(id, {}).get("error", "Missing execution result")}
            record["pred_sql"] = pred[id].get("predicted_sql", "")
            record["status"] = ("correct" if record["score"] else
                                "sql_error" if id in errors else "incorrect")
            scored.append(record)
        (run / "scored.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in scored), encoding="utf-8")
        correct = sum(int(r["score"]) for r in scored)
        summary = {"dataset": "Spider 2.0-Lite local SQLite", "subset": "135 local instances",
                   "evaluator": "official Spider 2.0-Lite CSV comparator; SQLite read-only execution with enforced timeout",
                   "timeout_seconds": args.timeout, "evaluated": len(scored), "correct": correct,
                   "ex_percent": 100 * correct / len(scored),
                   "sql_execution_errors": sum(r["status"] == "sql_error" for r in scored),
                   "generated_beijing": datetime.now(BEIJING).isoformat(timespec="seconds")}
        (run / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{name}: {correct}/{len(scored)} = {summary['ex_percent']:.2f}%", flush=True)
    tee = sys.stdout
    sys.stdout, sys.stderr = original_stdout, original_stderr
    tee.close()


if __name__ == "__main__":
    main()
