"""Check archived outputs and their paired statistics without API or databases."""
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "experiments/pilot"))
from summarize_full import summarize


def load(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def main():
    manifest = json.loads((ROOT / "results_manifest.json").read_text(encoding="utf-8"))
    for name, expected in manifest["sha256"].items():
        actual = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f"Archived file hash mismatch: {name}")
    directory = ROOT / "experiments/pilot/runs"
    predictions = load(directory / "full_official_prompt_deepseek-flash_max4096.jsonl")
    keys = {(r["question_id"], r["variant"]) for r in predictions}
    assert len(predictions) == len(keys) == 1000
    assert keys == {(qid, variant) for qid in range(500) for variant in ("baseline", "structured")}
    assert all(r["model"] == "deepseek-flash" and r["max_tokens"] == 4096
               and r["temperature"] == 0 and r["thinking"] == "disabled" for r in predictions)
    grades = load(directory / "full_official_eval/grades.jsonl")
    assert len(grades) == 500 and {r["question_id"] for r in grades} == set(range(500))
    assert all(r["gold_ok"] for r in grades)
    by_key = {(r["question_id"], r["variant"]): r for r in predictions}
    assert all(by_key[(r["question_id"], v)]["db_id"] == r["db_id"]
               for r in grades for v in ("baseline", "structured"))
    n, baseline, structured, bo, so, delta, p = summarize(grades)
    assert (n, baseline, structured, bo, so) == (500, 210, 223, 49, 62)
    for variant, percent in (("baseline", "41.80"), ("structured", "44.20")):
        text = (directory / f"full_official_eval/{variant}_official.txt").read_text(encoding="utf-8")
        scores = next(line.split() for line in text.splitlines() if line.startswith("EX "))
        assert scores[-1] == percent
    print("PASS: archived hashes, 1000 paired predictions, 500 local grades, official summaries")
    print(f"Local EX: {baseline}/{n} vs {structured}/{n}; delta={delta:+.2f} pp; p={p:.4f}")
    print("This check does not re-execute SQL or make API requests.")


if __name__ == "__main__":
    main()
