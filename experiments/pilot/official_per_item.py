"""Re-run BIRD's unmodified execute_model to retain its per-question EX scores."""

import json
from math import comb
from multiprocessing import Pool
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = ROOT / "sources/mini_dev/evaluation"
sys.path.insert(0, str(EVAL_DIR))
from evaluation_ex import execute_model
from evaluation_utils import package_sqls

OUT = ROOT / "experiments/pilot/runs/full_official_eval"
DB_ROOT = str(ROOT / "data/bird_mini_dev_db/dev_databases") + "/"


def exact_p(a, b):
    n = a + b
    if not n:
        return 1.0
    return min(1.0, 2 * sum(comb(n, i) for i in range(min(a, b) + 1)) / 2 ** n)


def main():
    gold, paths = package_sqls(str(OUT / "gold.sql"), DB_ROOT, mode="gt")
    assert len(gold) == len(paths) == 500
    by_variant = {}
    for variant in ("baseline", "structured"):
        cache = OUT / f"{variant}_official_items.json"
        if cache.exists():
            by_variant[variant] = json.loads(cache.read_text(encoding="utf-8"))
            assert len(by_variant[variant]) == 500
            print(variant, sum(by_variant[variant]), "loaded saved per-item scores", flush=True)
            continue
        pred, _ = package_sqls(str(OUT / f"{variant}.json"), DB_ROOT, mode="pred")
        assert len(pred) == 500
        jobs = [(pred[i], gold[i], paths[i], i, 30.0, "SQLite") for i in range(500)]
        with Pool(processes=2) as pool:
            results = pool.starmap(execute_model, jobs, chunksize=1)
        results.sort(key=lambda r: r["sql_idx"])
        by_variant[variant] = [bool(r["res"]) for r in results]
        cache.write_text(json.dumps(by_variant[variant]), encoding="utf-8")
        print(variant, sum(by_variant[variant]), flush=True)
    local = [json.loads(x) for x in (OUT / "grades.jsonl").read_text(encoding="utf-8").splitlines() if x]
    pilot_ids = {x["question_id"] for x in json.loads((ROOT / "experiments/pilot/selection.json").read_text(encoding="utf-8"))["items"]}
    rows = []
    for i in range(500):
        rows.append({
            "question_id": i,
            "difficulty": local[i]["difficulty"],
            "baseline": by_variant["baseline"][i],
            "structured": by_variant["structured"][i],
            "baseline_local": local[i]["baseline_match"],
            "structured_local": local[i]["structured_match"],
            "pilot": i in pilot_ids,
        })
    (OUT / "official_item_scores.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    lines = ["# Official BIRD paired EX result", "", "The unmodified official `execute_model` scored each frozen prediction with its standard 30-second combined query timeout.", "", "| Group | n | Baseline | Structured | Baseline only | Structured only | Delta (pp) | Exact paired p |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for name, group in (("All 500", rows), ("Excluding 20 pilot questions", [r for r in rows if not r["pilot"]])):
        n = len(group)
        b = sum(r["baseline"] for r in group)
        s = sum(r["structured"] for r in group)
        bo = sum(r["baseline"] and not r["structured"] for r in group)
        so = sum(r["structured"] and not r["baseline"] for r in group)
        lines.append(f"| {name} | {n} | {b}/{n} ({b/n:.2%}) | {s}/{n} ({s/n:.2%}) | {bo} | {so} | {100*(s-b)/n:+.2f} | {exact_p(bo, so):.4g} |")
    mismatches = [(r["question_id"], v) for r in rows for v in ("baseline", "structured") if r[v] != r[v+"_local"]]
    lines += ["", f"Official/local per-question mismatches: {mismatches}.", "", "This is exploratory because the checklist was tried in a preceding 20-question pilot. No human feedback was collected."]
    (OUT / "official_paired_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines), flush=True)


if __name__ == "__main__":
    main()
