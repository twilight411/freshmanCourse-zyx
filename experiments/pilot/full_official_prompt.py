"""Paired BIRD Mini-Dev inference using its published fixed SQLite prompt.

This script never reads gold SQL. Evaluation is a separate export step.
"""

import hashlib
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pilot import ApiError, ApiSettings, call_api, extract_sql, STRUCTURED_GUIDANCE


ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data/official_baseline_input_sqlite_2026.jsonl"
OUT = ROOT / "experiments/pilot/runs/full_official_prompt_deepseek-flash_max4096.jsonl"
EXPECTED_SHA256 = "b060ee9cb69be62a052f5e40367afeb2cf08f345f15ca318fd9a0f9bf52d53ef"
MODEL = "deepseek-flash"
SYSTEM = ""


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def main():
    assert sha256(DATA.read_bytes()) == EXPECTED_SHA256, "official prompt file changed"
    source = [json.loads(line) for line in DATA.read_text(encoding="utf-8").splitlines() if line]
    assert len(source) == 500
    questions = []
    for row in source:
        # Project only public inference fields; gold SQL never enters a job.
        questions.append((int(row["question_id"]), str(row["db_id"]), str(row["prompt"])))
    assert {qid for qid, _, _ in questions} == set(range(500))
    token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip()
    assert token and "\n" not in token
    settings = ApiSettings(
        base_url="https://api.deepseek.com/anthropic",
        model=MODEL,
        max_tokens=4096,
        temperature=0.0,
        api_timeout=120.0,
        auth_header="bearer",
        allow_http=False,
    )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    completed = set()
    if OUT.exists():
        for line in OUT.read_text(encoding="utf-8").splitlines():
            if line:
                record = json.loads(line)
                completed.add((record["question_id"], record["variant"]))
    consecutive_errors = 0
    with OUT.open("a", encoding="utf-8", buffering=1) as handle:
        for n, (qid, db_id, official_prompt) in enumerate(questions, 1):
            for variant in ("baseline", "structured"):
                if (qid, variant) in completed:
                    continue
                if variant == "baseline":
                    prompt = official_prompt
                else:
                    marker = "Output Format:\n"
                    assert official_prompt.count(marker) == 1
                    prompt = official_prompt.replace(
                        marker,
                        "Structured schema check (apply before the final answer):\n"
                        + STRUCTURED_GUIDANCE + "\n\n" + marker,
                        1,
                    )
                record = {
                    "question_id": qid,
                    "db_id": db_id,
                    "variant": variant,
                    "model": MODEL,
                    "temperature": 0.0,
                    "thinking": "disabled",
                    "max_tokens": 4096,
                    "official_prompt_sha256": sha256(official_prompt.encode("utf-8")),
                    "request_prompt_sha256": sha256(prompt.encode("utf-8")),
                    "prompt_chars": len(prompt),
                }
                started = time.monotonic()
                try:
                    answer = call_api(SYSTEM, prompt, token, settings)
                    record.update({
                        "latency_ms": round((time.monotonic() - started) * 1000, 2),
                        "response_id": answer.response_id,
                        "stop_reason": answer.stop_reason,
                        "prompt_tokens_uncached": answer.prompt_tokens,
                        "output_tokens": answer.output_tokens,
                        "raw_response": answer.text,
                    })
                    sql, err = extract_sql(answer.text)
                    record["sql"] = sql
                    record["error"] = err
                    consecutive_errors = 0
                except ApiError as exc:
                    record["latency_ms"] = round((time.monotonic() - started) * 1000, 2)
                    record["sql"] = None
                    record["error"] = str(exc).replace(token, "[REDACTED]")
                    consecutive_errors += 1
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                print(f"[{n}/500] qid={qid} {variant}: " + ("SQL" if record["sql"] else "ERROR"), flush=True)
                if consecutive_errors >= 3:
                    raise RuntimeError("three consecutive API errors; stopped with resumable partial output")
    print("Completed 1000 paired calls; evaluate separately with the official scorer.")


if __name__ == "__main__":
    main()
