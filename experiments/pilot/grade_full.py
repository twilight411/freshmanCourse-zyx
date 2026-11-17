"""Score frozen full-run predictions and export them to the official BIRD EX scorer."""

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pilot import ExecConfig, execute_sql, resolve_db_path


ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data/mini_dev_sqlite_2026.jsonl"
PRED = ROOT / "experiments/pilot/runs/full_official_prompt_deepseek-flash_max4096.jsonl"
OUT = ROOT / "experiments/pilot/runs/full_official_eval"
DB_ROOT = ROOT / "data/bird_mini_dev_db/dev_databases"


def load_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def main():
    predictions = load_jsonl(PRED)
    by_key = {(r["question_id"], r["variant"]): r for r in predictions}
    assert len(predictions) == 1000 and len(by_key) == 1000, "full run incomplete or duplicated"
    dataset = {r["question_id"]: r for r in load_jsonl(DATA)}
    assert len(dataset) == 500
    OUT.mkdir(parents=True, exist_ok=True)
    gold_lines, diff_lines = [], []
    pred_files = {v: {} for v in ("baseline", "structured")}
    grades = []
    config = ExecConfig(timeout_s=30.0, max_vm_steps=0, max_rows=200_000)
    for qid in range(500):
        row = dataset[qid]
        db_id = row["db_id"]
        db_path = resolve_db_path(DB_ROOT, db_id)
        # BIRD's official loader reads one physical line per query. Three
        # published gold queries span multiple lines, so fold their newlines
        # to spaces without changing SQL semantics.
        gold_one_line = row["SQL"].replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
        gold_lines.append(f'{gold_one_line}\t{db_id}\n')
        diff_lines.append(json.dumps({"difficulty": row["difficulty"]}) + "\n")
        gold = execute_sql(db_path, row["SQL"], config)
        grade = {"question_id": qid, "db_id": db_id, "difficulty": row["difficulty"], "gold_ok": gold.success}
        for variant in ("baseline", "structured"):
            pred = by_key[(qid, variant)]
            assert pred["db_id"] == db_id
            sql = pred.get("sql") or "SELECT 1 WHERE 0"
            pred_files[variant][str(qid)] = sql + "\t----- bird -----\t" + db_id
            result = execute_sql(db_path, sql, config) if pred.get("sql") else None
            grade[variant + "_ok"] = bool(result and result.success)
            grade[variant + "_match"] = bool(result and gold.success and result.success and set(result.rows) == set(gold.rows))
            grade[variant + "_error"] = (result.error if result else pred.get("error"))
        grades.append(grade)
        if (qid + 1) % 50 == 0:
            print(f"graded {qid + 1}/500", flush=True)
    (OUT / "gold.sql").write_text("".join(gold_lines), encoding="utf-8")
    (OUT / "difficulty.jsonl").write_text("".join(diff_lines), encoding="utf-8")
    for variant, pred in pred_files.items():
        (OUT / f"{variant}.json").write_text(json.dumps(pred, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "grades.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in grades), encoding="utf-8")
    print("gold_ok", sum(r["gold_ok"] for r in grades))
    for variant in ("baseline", "structured"):
        print(variant, sum(r[variant + "_match"] for r in grades))
    print("baseline_only", sum(r["baseline_match"] and not r["structured_match"] for r in grades))
    print("structured_only", sum(r["structured_match"] and not r["baseline_match"] for r in grades))


if __name__ == "__main__":
    main()
