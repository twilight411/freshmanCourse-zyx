"""Run the BIRD-style single-turn model setup on Spider 2.0-Lite local SQLite tasks."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from openai import OpenAI


ROOT = Path(__file__).resolve().parent
LITE = ROOT / "spider2-lite"
RUNS = ROOT / "runs"
BEIJING = timezone(timedelta(hours=8))
FENCE = re.compile(r"```(?:sql|sqlite)?[ \t]*\r?\n(.*?)```", re.I | re.S)
QUERY_START = re.compile(r"^\s*(?:--[^\n]*\n\s*)*(?:SELECT|WITH)\b", re.I)
MODELS = {
    "deepseek": {
        "model": "deepseek-flash",
        "base_url": "https://api.deepseek.com",
        "api_key_env": "DEEPSEEK_API_KEY",
        "thinking": "enabled",
        "reasoning_effort": "high",
        "workers": 4,
    },
    "qwen": {
        "model": "qwen-flash",
        "base_url": "https://maas.whu.edu.cn/v1",
        "api_key_env": "WHU_MAAS_API_KEY",
        "thinking": "disabled",
        "max_tokens": 4096,
        "workers": 6,
    },
    "deepseek_whu_nonthinking": {
        "model": "deepseek-flash",
        "base_url": "https://maas.whu.edu.cn/v1",
        "api_key_env": "WHU_MAAS_API_KEY",
        "thinking": "disabled",
        "max_tokens": 4096,
        "workers": 6,
    },
}


def now() -> str:
    return datetime.now(BEIJING).isoformat(timespec="seconds")


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_keys() -> None:
    path = Path(r"D:\Bird-Benchmark\.env")
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def metadata_dir(db: str) -> Path:
    base = LITE / "resource/databases/sqlite"
    normalized = re.sub(r"[^a-z0-9]", "", db.casefold())
    matches = [p for p in base.iterdir() if p.is_dir()
               and re.sub(r"[^a-z0-9]", "", p.name.casefold()) == normalized]
    if len(matches) != 1:
        raise FileNotFoundError(f"No unique schema directory for {db}")
    return matches[0]


def prompt_for(item: dict) -> str:
    with (metadata_dir(item["db"]) / "DDL.csv").open(encoding="utf-8-sig", newline="") as f:
        ddls = [row["DDL"] for row in csv.DictReader(f)]
    doc = item.get("external_knowledge")
    knowledge = ""
    if doc:
        path = LITE / "resource/documents" / doc
        if not path.is_file():
            raise FileNotFoundError(f"Missing external knowledge for {item['instance_id']}: {path}")
        knowledge = path.read_text(encoding="utf-8")
    return ("Task Overview:\nYou are a data science expert. Below, you are provided with a database "
            "schema and a natural language question. Your task is to understand the schema and generate "
            "a valid SQL query to answer the question.\n\nDatabase Engine:\nSQLite\n\nDatabase Schema:\n"
            + "\n\n".join(ddls) + "\n\nQuestion:\n" + item["question"]
            + "\n\nExternal Knowledge:\n" + (knowledge or "None")
            + "\n\nInstructions:\n- Make sure you only output the information that is asked in the question. "
            "If the question asks for a specific column, make sure to only include that column in the SELECT clause, nothing more.\n"
            "- The generated query should return all of the information asked in the question without any missing or extra information.\n"
            "- Before generating the final SQL query, please think through the steps of how to write the query.\n\n"
            "Output Format:\nIn your answer, please enclose the generated SQL query in a code block:\n"
            "```sql\n-- Your SQL query\n```\n\nTake a deep breath and think step by step to find the correct SQL query.\n")


def extract_sql(content: str) -> str:
    blocks = [match.group(1).strip() for match in FENCE.finditer(content or "")]
    complete = [block for block in blocks if QUERY_START.match(block)]
    if complete:
        return complete[-1]
    return (blocks[-1] if blocks else content or "").strip()


def ensure_db_links() -> None:
    source = LITE / "resource/databases/spider2-localdb"
    dest = LITE / "resource/databases"
    for db in source.glob("*.sqlite"):
        target = dest / db.name
        if not target.exists():
            os.link(db, target)


def run_model(name: str, limit: int | None) -> None:
    cfg = MODELS[name]
    load_keys()
    key = os.environ.get(cfg["api_key_env"])
    if not key:
        raise RuntimeError(f"Missing {cfg['api_key_env']}")
    examples = [x for x in read_jsonl(LITE / "spider2-lite.jsonl") if x["instance_id"].startswith("local")]
    assert len(examples) == 135
    if limit:
        examples = examples[:limit]
    run = RUNS / f"spider2_lite_local_{name}"
    run.mkdir(parents=True, exist_ok=True)
    sql_dir = run / "sql"
    sql_dir.mkdir(exist_ok=True)
    prompts = {x["instance_id"]: prompt_for(x) for x in examples}
    (run / "prompts.jsonl").write_text("".join(json.dumps({"instance_id": x["instance_id"], "db": x["db"],
        "question": x["question"], "external_knowledge": x.get("external_knowledge"),
        "prompt": prompts[x["instance_id"]]}, ensure_ascii=False) + "\n" for x in examples), encoding="utf-8")
    (run / "run_config.json").write_text(json.dumps({"dataset": "Spider 2.0-Lite local SQLite",
        "dataset_version": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "subset": "135 local instances", "model": cfg["model"], "base_url": cfg["base_url"],
        "thinking": cfg["thinking"], "reasoning_effort": cfg.get("reasoning_effort"),
        "max_tokens": cfg.get("max_tokens"), "created_beijing": now()}, ensure_ascii=False, indent=2), encoding="utf-8")
    pred_path = run / "predictions.jsonl"
    previous = {x["instance_id"]: x for x in read_jsonl(pred_path)} if pred_path.exists() else {}
    pending = [x for x in examples if x["instance_id"] not in previous or previous[x["instance_id"]].get("api_error")
               or not previous[x["instance_id"]].get("predicted_sql")]
    client = OpenAI(api_key=key, base_url=cfg["base_url"], timeout=300 if name == "deepseek" else 180, max_retries=2)

    def call(item: dict) -> dict:
        rec = {"instance_id": item["instance_id"], "db": item["db"], "model": cfg["model"], "timestamp_beijing": now()}
        try:
            req = {"model": cfg["model"], "messages": [{"role": "user", "content": prompts[item["instance_id"]]}]}
            if cfg["thinking"] == "enabled":
                req["extra_body"] = {"thinking": {"type": "enabled"}}
                req["reasoning_effort"] = "high"
            else:
                req["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
                req["max_tokens"] = cfg["max_tokens"]
            response = client.chat.completions.create(**req)
            content = response.choices[0].message.content or ""
            usage = response.usage
            rec.update(raw_response=content, predicted_sql=extract_sql(content), api_model=response.model,
                       finish_reason=response.choices[0].finish_reason,
                       input_tokens=usage.prompt_tokens if usage else 0,
                       output_tokens=usage.completion_tokens if usage else 0)
        except Exception as exc:
            rec.update(api_error=f"{type(exc).__name__}: {str(exc)[:500]}", predicted_sql="", input_tokens=0, output_tokens=0)
        return rec

    print(f"[{now()}] {name}: {len(previous)} saved, {len(pending)} pending", flush=True)
    with pred_path.open("a", encoding="utf-8") as output, ThreadPoolExecutor(max_workers=cfg["workers"]) as pool:
        for future in as_completed([pool.submit(call, item) for item in pending]):
            rec = future.result()
            output.write(json.dumps(rec, ensure_ascii=False) + "\n")
            output.flush()
            previous[rec["instance_id"]] = rec
            print(f"[{now()}] {name} {len(previous)}/{len(examples)} {rec['instance_id']} "
                  f"{'API error' if rec.get('api_error') else 'saved'}", flush=True)
    for item in examples:
        rec = previous[item["instance_id"]]
        (sql_dir / f"{item['instance_id']}.sql").write_text(rec.get("predicted_sql", ""), encoding="utf-8")
    failed = [id for id, r in previous.items() if r.get("api_error") or not r.get("predicted_sql")]
    print(f"[{now()}] {name}: SQL files={len(examples)}, API/empty={len(failed)}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", choices=["deepseek", "qwen", "deepseek_whu_nonthinking", "both"], default="both")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    ensure_db_links()
    for name in ([args.model] if args.model != "both" else ["deepseek", "qwen"]):
        run_model(name, args.limit)


if __name__ == "__main__":
    main()
