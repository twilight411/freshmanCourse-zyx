"""Write a compact paired summary for the 500-question official-prompt run."""

import json
from math import comb
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GRADES = ROOT / "experiments/pilot/runs/full_official_eval/grades.jsonl"
SELECTION = ROOT / "experiments/pilot/selection.json"
OUT = ROOT / "experiments/pilot/runs/full_official_eval/summary.md"


def summarize(rows):
    n = len(rows)
    baseline = sum(r["baseline_match"] for r in rows)
    structured = sum(r["structured_match"] for r in rows)
    b_only = sum(r["baseline_match"] and not r["structured_match"] for r in rows)
    s_only = sum(r["structured_match"] and not r["baseline_match"] for r in rows)
    discordant = b_only + s_only
    if discordant:
        tail = sum(comb(discordant, i) for i in range(min(b_only, s_only) + 1))
        p = min(1.0, 2 * tail / 2 ** discordant)
    else:
        p = 1.0
    return n, baseline, structured, b_only, s_only, 100 * (structured - baseline) / n, p


def main():
    rows = [json.loads(x) for x in GRADES.read_text(encoding="utf-8").splitlines() if x]
    assert len(rows) == 500
    pilot_ids = {x["question_id"] for x in json.loads(SELECTION.read_text(encoding="utf-8"))["items"]}
    groups = {
        "All 500": rows,
        "Excluding the 20 engineering-pilot questions": [r for r in rows if r["question_id"] not in pilot_ids],
        "simple": [r for r in rows if r["difficulty"] == "simple"],
        "moderate": [r for r in rows if r["difficulty"] == "moderate"],
        "challenging": [r for r in rows if r["difficulty"] == "challenging"],
    }
    text = [
        "# BIRD Mini-Dev 2026: fixed-prompt paired run",
        "",
        "The baseline sends BIRD's published fixed SQLite prompt unchanged. The structured arm inserts one checklist before its Output Format section. Both use DeepSeek `deepseek-flash`, temperature 0, thinking disabled, and max 4096 output tokens. The checklist was already explored on a 20-question pilot, so this is an exploratory validation, not a preregistered independent test. It contains no human feedback.",
        "",
        "| Group | n | Baseline EX | Structured EX | Baseline only | Structured only | Delta (pp) | Exact paired p |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, group in groups.items():
        n, b, s, bo, so, delta, p = summarize(group)
        text.append(f"| {name} | {n} | {b}/{n} ({100*b/n:.2f}%) | {s}/{n} ({100*s/n:.2f}%) | {bo} | {so} | {delta:+.2f} | {p:.4g} |")
    text += [
        "",
        f"Gold SQL executed successfully in {sum(r['gold_ok'] for r in rows)}/500 cases in the local scorer.",
        "The official BIRD evaluator's aggregate outputs are in `baseline_official.txt` and `structured_official.txt`; compare these with the local counts before using the result.",
        "Execution accuracy uses unordered set equality, matching the official scorer. A 500-question public benchmark result is not an independent held-out generalization estimate.",
        "",
    ]
    OUT.write_text("\n".join(text), encoding="utf-8")
    print("\n".join(text))


if __name__ == "__main__":
    main()
