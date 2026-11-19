"""Run a resumable, zero-shot BEAVER sample through the WHU DeepSeek and Qwen API."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from openai import OpenAI


ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "runs"
BEIJING = timezone(timedelta(hours=8))
SPLITS = ("dw", "dw_real", "neutron", "nova")
FENCE = re.compile(r"```(?:sql|mysql)?\s*\n?(.*?)```", re.I | re.S)
TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]*")
STOP = {"the", "and", "with", "from", "for", "that", "this", "each", "are", "has", "how", "what",
        "which", "whose", "into", "over", "than", "all", "show", "list", "give", "find", "get",
        "number", "total", "count", "name", "names", "table", "value", "values"}
MODELS = {
    "deepseek": {"id": "deepseek-flash", "thinking": "disabled", "max_tokens": 4096,
                 "input_rate": 20, "output_rate": 80, "workers": 6},
    "qwen": {"id": "qwen-flash", "thinking": "disabled", "max_tokens": 4096,
             "input_rate": 10, "output_rate": 30, "workers": 6},
}


def now() -> str:
    return datetime.now(BEIJING).isoformat(timespec="seconds")


def load_env() -> None:
    for path in (ROOT / ".env", Path(r"D:\Bird-Benchmark\.env")):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] if path.is_file() else []


def words(value: str) -> list[str]:
    # Split snake_case and CamelCase names before ranking relevant tables.
    value = re.sub(r"([a-z])([A-Z])", r"\1 \2", value).replace("_", " ")
    return [x.lower() for x in TOKEN.findall(value) if len(x) > 2 and x.lower() not in STOP]


def names_and_types(table: dict) -> list[tuple[str, str]]:
    names = table.get("column_names") or []
    types = table.get("column_types") or []
    if isinstance(names, str):
        names = json.loads(names)
    if isinstance(types, str):
        types = json.loads(types)
    if isinstance(names, dict):
        names = list(names.values())
    if isinstance(types, dict):
        types = list(types.values())
    return [(str(name), str(types[i]) if i < len(types) else "TEXT") for i, name in enumerate(names)]


def rank_tables(question: str, tables: dict, limit: int = 15) -> list[dict]:
    query = Counter(words(question))
    if not query:
        return list(tables.values())[:limit]
    docs = {}
    counts = Counter()
    for name, table in tables.items():
        title = Counter(words(name))
        columns = Counter(word for col, _ in names_and_types(table) for word in words(col))
        docs[name] = (title, columns)
        counts.update(set(title) | set(columns))
    n = len(tables)
    scored = []
    for name, table in tables.items():
        title, columns = docs[name]
        score = 0.0
        for word, count in query.items():
            idf = math.log(1 + (n + 1) / (counts[word] + 1))
            score += min(count, 2) * idf * (4 * min(title[word], 2) + min(columns[word], 5))
        scored.append((score, name, table))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [table for _, _, table in scored[:limit]]


def quote(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def table_schema(table: dict) -> str:
    name = str(table.get("table_name") or "unknown")
    columns = names_and_types(table)
    ddl = "CREATE TABLE " + quote(name) + " (\n" + ",\n".join(
        "  " + quote(col) + " " + typ for col, typ in columns) + "\n);"
    examples = table.get("example_rows") or []
    if isinstance(examples, str):
        try:
            examples = json.loads(examples)
        except json.JSONDecodeError:
            examples = []
    examples = examples[:2] if isinstance(examples, list) else []
    return ddl + ("\nExample rows: " + json.dumps(examples, ensure_ascii=False)[:1400] if examples else "")


def build_prompt(question: dict, tables: dict, top_k: int) -> tuple[str, list[str]]:
    selected = rank_tables(question["question"], tables, top_k)
    # Keep each prompt within a known local budget while preserving chosen table DDL.
    schema = "\n\n".join(table_schema(t) for t in selected)
    if len(schema) > 48000:
        schema = schema[:48000] + "\n-- Further schema text omitted at the prompt size limit."
    selected_names = [str(t.get("table_name")) for t in selected]
    prompt = ("Task Overview:\nYou are a data science expert. Below, you are provided with a database "
              "schema and a natural language question. Understand the schema and generate one valid SQL "
              "query to answer the question.\n\nDatabase Engine:\nMySQL 8\n\nDatabase Schema "
              "(retrieved from the provided table metadata; these are candidate tables, not gold hints):\n"
              + schema + "\n\nQuestion:\n" + question["question"]
              + "\n\nInstructions:\n- Return only the information asked for, with no missing or extra columns.\n"
              "- Use only columns and tables shown in the schema. If a needed table may be absent, do your best "
              "with the available metadata.\n- Before writing the final SQL, think through the joins, filters, "
              "aggregation and output shape.\n\nOutput Format:\nEnclose the final query in a SQL code block:\n"
              "```sql\n-- Your SQL query\n```\n")
    return prompt, selected_names


def extract_sql(content: str) -> str:
    blocks = [match.strip() for match in FENCE.findall(content or "")]
    if not blocks:
        return (content or "").strip()
    candidates = []
    for block in blocks:
        start = re.sub(r"^(?:\s*--[^\n]*\n|\s*/\*.*?\*/\s*)*", "", block, flags=re.S)
        if re.match(r"^(SELECT|WITH|\()\b", start, re.I) and "..." not in block:
            candidates.append(block)
    return candidates[-1] if candidates else blocks[-1]


def all_credit_usage() -> float:
    total = 0.0
    for model_name, cfg in MODELS.items():
        for split in SPLITS:
            path = RUNS / f"{split}_{model_name}" / "predictions.jsonl"
            for row in read_jsonl(path):
                total += (row.get("input_tokens", 0) * cfg["input_rate"]
                          + row.get("output_tokens", 0) * cfg["output_rate"]) / 1_000_000
    return total


def run_split(split: str, model_name: str, limit: int, top_k: int, max_credits: float) -> None:
    cfg = MODELS[model_name]
    run = RUNS / f"{split}_{model_name}"
    run.mkdir(parents=True, exist_ok=True)
    questions = json.loads((ROOT / "data" / split / "dev_sampled.json").read_text(encoding="utf-8"))[:limit]
    tables = json.loads((ROOT / "data" / ("dw" if split == "dw_real" else split) / "dev_tables.json").read_text(encoding="utf-8"))
    prepared = []
    for q in questions:
        prompt, selected = build_prompt(q, tables, top_k)
        prepared.append({"id": str(q["id"]), "db": q["db"], "question": q["question"],
                         "prompt": prompt, "selected_tables": selected})
    (run / "prompts.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in prepared), encoding="utf-8")
    (run / "run_config.json").write_text(json.dumps({"dataset": "BEAVER", "split": split,
        "sample_seed": 77, "sample_count": len(questions), "method": "zero-shot lexical table retrieval",
        "top_k": top_k, "model": cfg["id"], "base_url": "https://maas.whu.edu.cn/v1",
        "thinking": cfg["thinking"], "reasoning_effort": cfg.get("reasoning_effort"),
        "max_tokens": cfg.get("max_tokens"), "created_beijing": now()}, ensure_ascii=False, indent=2), encoding="utf-8")
    predictions = run / "predictions.jsonl"
    previous = {str(x["id"]): x for x in read_jsonl(predictions)}
    pending = [x for x in prepared if x["id"] not in previous or not previous[x["id"]].get("predicted_sql")]
    client = OpenAI(api_key=os.environ["WHU_MAAS_API_KEY"], base_url="https://maas.whu.edu.cn/v1",
                    timeout=300 if model_name == "deepseek" else 180, max_retries=2)

    def call(item: dict) -> dict:
        row = {"id": item["id"], "db": item["db"], "model": cfg["id"], "timestamp_beijing": now()}
        try:
            request = {"model": cfg["id"], "messages": [{"role": "user", "content": item["prompt"]}]}
            if model_name == "deepseek":
                request["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
                request["max_tokens"] = cfg["max_tokens"]
            else:
                request["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
                request["max_tokens"] = cfg["max_tokens"]
            response = client.chat.completions.create(**request)
            text = response.choices[0].message.content or ""
            usage = response.usage
            row.update(raw_response=text, predicted_sql=extract_sql(text), api_model=response.model,
                       finish_reason=response.choices[0].finish_reason,
                       input_tokens=usage.prompt_tokens if usage else 0,
                       output_tokens=usage.completion_tokens if usage else 0)
        except Exception as exc:
            row.update(api_error=f"{type(exc).__name__}: {str(exc)[:500]}", predicted_sql="",
                       input_tokens=0, output_tokens=0)
        return row

    print(f"[{now()}] {split}/{model_name}: {len(previous)} saved, {len(pending)} pending", flush=True)
    with predictions.open("a", encoding="utf-8") as output:
        while pending:
            credits = all_credit_usage()
            if credits >= max_credits:
                print(f"[{now()}] Stopped at estimated {credits:.2f}/{max_credits:.2f} incremental credits", flush=True)
                break
            batch, pending = pending[:cfg["workers"]], pending[cfg["workers"]:]
            with ThreadPoolExecutor(max_workers=cfg["workers"]) as pool:
                for future in as_completed([pool.submit(call, item) for item in batch]):
                    row = future.result()
                    output.write(json.dumps(row, ensure_ascii=False) + "\n")
                    output.flush()
                    previous[row["id"]] = row
                    print(f"[{now()}] {split}/{model_name} {len(previous)}/{len(prepared)} {row['id']}: "
                          f"{'API error' if row.get('api_error') else 'saved'}", flush=True)
    print(f"[{now()}] {split}/{model_name}: {len(previous)}/{len(prepared)}; "
          f"estimated incremental credits={all_credit_usage():.2f}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-size", type=int, default=100)
    parser.add_argument("--top-k", type=int, default=15)
    parser.add_argument("--max-credits", type=float, default=172.5)
    parser.add_argument("--model", choices=("deepseek", "qwen", "both"), default="both")
    parser.add_argument("--split", choices=(*SPLITS, "all"), default="all")
    args = parser.parse_args()
    load_env()
    if not os.getenv("WHU_MAAS_API_KEY"):
        raise SystemExit("WHU_MAAS_API_KEY missing")
    if args.sample_size < 1 or args.top_k < 1 or args.max_credits <= 0:
        parser.error("sample-size, top-k and max-credits must be positive")
    models = MODELS if args.model == "both" else {args.model: MODELS[args.model]}
    splits = SPLITS if args.split == "all" else (args.split,)
    for split in splits:
        for name in models:
            run_split(split, name, args.sample_size, args.top_k, args.max_credits)


if __name__ == "__main__":
    main()
