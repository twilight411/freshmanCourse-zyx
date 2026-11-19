"""Run DeepSeek on BIRD SQLite and MySQL splits and build HTML reports."""

from __future__ import annotations

import argparse
import heapq
import html
import math
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DATASETS = {
    "mini_sqlite": {
        "prompts": ROOT / "data/mini_dev_2026/baseline_inference/mini_dev_sqlite.jsonl",
        "databases": ROOT / "data/bird_dev/dev_20240627/dev_databases",
        "expected": 500,
        "name": "BIRD Mini-Dev 2026 SQLite",
        "source": "birdsql/bird_mini_dev",
        "split": "mini_dev_sqlite",
        "release": "Mini-Dev 1024 (2026 update)",
    },
    "full_dev_sqlite": {
        "prompts": ROOT / "data/bird_dev/dev_20240627/full_dev_prompts.jsonl",
        "databases": ROOT / "data/bird_dev/dev_20240627/dev_databases",
        "expected": 1534,
        "name": "BIRD Dev 2024 SQLite",
        "source": "BIRD official dev.zip",
        "split": "dev_20240627",
        "release": "dev_20240627",
    },
    "revised_dev_sqlite": {
        "prompts": ROOT / "data/bird_dev/dev_20251106/revised_dev_prompts.jsonl",
        "databases": ROOT / "data/bird_dev/dev_20240627/dev_databases",
        "expected": 1534,
        "name": "BIRD revised Dev 2025 SQLite",
        "source": "birdsql/bird_sql_dev_20251106",
        "split": "dev_20251106",
        "release": "2025-11-06 official revision",
    },
    "mini_mysql": {
        "prompts": ROOT / "data/mini_dev_2026/baseline_inference/mini_dev_mysql.jsonl",
        "databases": None,
        "expected": 500,
        "name": "BIRD Mini-Dev 2026 MySQL",
        "source": "birdsql/bird_mini_dev",
        "split": "mini_dev_mysql",
        "release": "Mini-Dev 1024 (2026 update)",
        "engine": "mysql",
    },
    "mini_postgresql": {
        "prompts": ROOT / "data/mini_dev_2026/baseline_inference/mini_dev_postgresql.jsonl",
        "databases": None,
        "expected": 500,
        "name": "BIRD Mini-Dev 2026 PostgreSQL",
        "source": "birdsql/bird_mini_dev",
        "split": "mini_dev_pg",
        "release": "Mini-Dev 1024 (2026 update)",
        "engine": "postgresql",
    },
    "full_dev_mysql": {
        "prompts": ROOT / "data/bird_dev/dev_20240627/full_dev_mysql_prompts.jsonl",
        "databases": None,
        "expected": 1534,
        "name": "BIRD Dev 2024 MySQL (UniQL port)",
        "source": "BIRD dev.zip + UniQL MySQL annotations + Mini-Dev MySQL database",
        "split": "dev_20240627_mysql_uniql",
        "release": "local cross-dialect port",
        "engine": "mysql",
    },
}
PROMPTS = DATASETS["mini_sqlite"]["prompts"]
DATABASES = DATASETS["mini_sqlite"]["databases"]
ACTIVE_DATASET = DATASETS["mini_sqlite"]
BEIJING = timezone(timedelta(hours=8))
FENCE = re.compile(r"```(?:sql|sqlite)?\s*\n?(.*?)```", re.IGNORECASE | re.DOTALL)


def select_dataset(key: str) -> None:
    global PROMPTS, DATABASES, ACTIVE_DATASET
    ACTIVE_DATASET = DATASETS[key]
    PROMPTS = ACTIVE_DATASET["prompts"]
    DATABASES = ACTIVE_DATASET["databases"]


def dataset_key_from_config(config: dict) -> str:
    if config.get("dataset_key"):
        return config["dataset_key"]
    return {"dev_20240627": "full_dev_sqlite", "dev_20251106": "revised_dev_sqlite", "mini_dev_mysql": "mini_mysql",
            "mini_dev_pg": "mini_postgresql",
            "dev_20240627_mysql_uniql": "full_dev_mysql"}.get(config.get("split"), "mini_sqlite")


def load_env() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_jsonl(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as source:
        return [json.loads(line) for line in source if line.strip()]


def append_jsonl(path: Path, record: dict) -> None:
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(record, ensure_ascii=False) + "\n")
        output.flush()


def extract_sql(content: str) -> str:
    match = FENCE.search(content or "")
    sql = match.group(1) if match else (content or "")
    sql = sql.strip()
    sql = re.sub(r"^\s*```(?:sql|sqlite)?\s*", "", sql, flags=re.IGNORECASE)
    return re.sub(r"\s*```\s*$", "", sql).strip()


def execute_readonly(sql: str, db_id: str, timeout_s: float = 30) -> set[tuple]:
    if ACTIVE_DATASET.get("engine") == "mysql":
        return execute_mysql_readonly(sql, timeout_s)
    if ACTIVE_DATASET.get("engine") == "postgresql":
        return execute_postgresql_readonly(sql, timeout_s)
    db = DATABASES / db_id / f"{db_id}.sqlite"
    if not db.is_file():
        raise FileNotFoundError(f"Database missing: {db}")
    if not re.match(r"^\s*(?:--[^\n]*\n\s*)*(SELECT|WITH)\b", sql, re.IGNORECASE):
        raise ValueError("Only SELECT/WITH queries are accepted")
    started = time.monotonic()
    connection = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        # Some Python SQLite builds omit the math extension used by BIRD gold SQL.
        connection.create_function("FLOOR", 1, math.floor, deterministic=True)

        def progress() -> int:
            return int(time.monotonic() - started > timeout_s)

        connection.set_progress_handler(progress, 10000)
        rows = connection.execute(sql).fetchall()
        return set(rows)  # BIRD's strict EX compares sets of result rows.
    finally:
        connection.close()


def mysql_credentials() -> dict[str, str]:
    path = ROOT / "mysql_runtime/.env"
    if not path.is_file():
        raise FileNotFoundError(f"MySQL credentials missing: {path}")
    return dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines()
                if line and "=" in line)


def execute_mysql_readonly(sql: str, timeout_s: float) -> set[tuple]:
    import pymysql

    if not re.match(r"^\s*(?:--[^\n]*\n\s*)*(SELECT|WITH)\b", sql, re.IGNORECASE):
        raise ValueError("Only SELECT/WITH queries are accepted")
    cred = mysql_credentials()
    connection = pymysql.connect(
        host=cred.get("BIRD_MYSQL_HOST", "127.0.0.1"),
        port=int(cred.get("BIRD_MYSQL_PORT", "33306")),
        user=cred["BIRD_MYSQL_USER"], password=cred["BIRD_MYSQL_PASSWORD"],
        database=cred["BIRD_MYSQL_DATABASE"], charset="utf8mb4",
        connect_timeout=10, read_timeout=int(timeout_s) + 15, autocommit=True,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET SESSION MAX_EXECUTION_TIME=%s", (int(timeout_s * 1000),))
            cursor.execute("SELECT @@SESSION.sql_mode")
            modes = [mode for mode in cursor.fetchone()[0].split(",") if mode != "ONLY_FULL_GROUP_BY"]
            cursor.execute("SET SESSION sql_mode=%s", (",".join(modes),))
            cursor.execute(sql)
            return set(cursor.fetchall())
    finally:
        connection.close()


def execute_postgresql_readonly(sql: str, timeout_s: float) -> set[tuple]:
    import psycopg2

    if not re.match(r"^\s*(?:--[^\n]*\n\s*)*(SELECT|WITH)\b", sql, re.IGNORECASE):
        raise ValueError("Only SELECT/WITH queries are accepted")
    path = ROOT / "postgres_runtime/.env"
    cred = dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines()
                if line and "=" in line)
    connection = psycopg2.connect(
        host=cred["BIRD_PG_HOST"], port=int(cred["BIRD_PG_PORT"]),
        user=cred["BIRD_PG_USER"], password=cred["BIRD_PG_PASSWORD"],
        dbname=cred["BIRD_PG_DATABASE"], connect_timeout=10,
    )
    try:
        connection.set_session(readonly=True)
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout = %s", (int(timeout_s * 1000),))
            cursor.execute(sql)
            return set(cursor.fetchall())
    finally:
        connection.close()


def row_preview(rows: set[tuple], limit: int = 5) -> list[list]:
    return [list(row) for row in heapq.nsmallest(limit, rows, key=repr)]


def score_record(record: dict, question: dict, timeout_s: float) -> dict:
    result = dict(record)
    result["question"] = question["question"]
    result["evidence"] = question.get("evidence", "")
    result["difficulty"] = question["difficulty"]
    result["gold_sql"] = question["SQL"]
    result["correct"] = False
    if record.get("api_error"):
        result["status"] = "api_error"
        return result
    if not record.get("predicted_sql"):
        result["status"] = "empty_sql"
        return result
    predicted = None
    if record.get("predicted_sql"):
        try:
            predicted = execute_readonly(record["predicted_sql"], question["db_id"], timeout_s)
            result["predicted_rows"] = len(predicted)
            result["predicted_sample"] = row_preview(predicted)
        except Exception as exc:
            result["prediction_error"] = str(exc)[:500]
    try:
        gold = execute_readonly(question["SQL"], question["db_id"], timeout_s)
        result["gold_rows"] = len(gold)
        result["gold_sample"] = row_preview(gold)
    except Exception as exc:
        result["status"] = "gold_error"
        result["reference_error"] = str(exc)[:500]
        result["error"] = result["reference_error"]
        return result
    if predicted is None:
        result["status"] = "sql_error"
        result["error"] = result.get("prediction_error", "Empty SQL")
        return result
    result["correct"] = predicted == gold
    result["status"] = "correct" if result["correct"] else "incorrect"
    if not result["correct"]:
        predicted_only = predicted - gold
        gold_only = gold - predicted
        result["only_predicted_rows"] = len(predicted_only)
        result["only_gold_rows"] = len(gold_only)
        result["only_predicted_sample"] = row_preview(predicted_only)
        result["only_gold_sample"] = row_preview(gold_only)
    return result


def latest_records(path: Path) -> dict[int, dict]:
    if not path.exists():
        return {}
    records = {}
    for record in load_jsonl(path):
        records[int(record["question_id"])] = record
    return records


def run(args: argparse.Namespace) -> None:
    select_dataset(args.dataset)
    load_env()
    key = os.environ.get(args.api_key_env)
    if not key:
        raise SystemExit(f"{args.api_key_env} is missing from .env or the environment")
    from openai import OpenAI

    questions = load_jsonl(PROMPTS)
    if len(questions) != ACTIVE_DATASET["expected"]:
        raise SystemExit(f"Expected {ACTIVE_DATASET['expected']} prompts; found {len(questions)}")
    if not 1 <= args.limit <= len(questions):
        raise SystemExit(f"--limit must be between 1 and {len(questions)}")
    run_dir = Path(args.run_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    predictions = run_dir / "predictions.jsonl"
    existing = latest_records(predictions)
    model = args.model or os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")
    base_url = args.base_url or os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    metadata_path = run_dir / "run_config.json"
    thinking_label = ("disabled" if args.qwen_nonthinking else
                      args.thinking if args.provider == "deepseek" else "provider-default")
    config = {"dataset_key": args.dataset, "dataset": ACTIVE_DATASET["source"],
              "split": ACTIVE_DATASET["split"],
              "release": ACTIVE_DATASET["release"], "model": model,
              "base_url": base_url, "thinking": thinking_label,
              "reasoning_effort": args.reasoning_effort if args.provider == "deepseek" and args.thinking == "enabled" else None,
              "provider": args.provider, "max_output_tokens": args.max_output_tokens,
              "qwen_nonthinking": args.qwen_nonthinking,
              "created_beijing": datetime.now(BEIJING).isoformat()}
    if metadata_path.exists():
        previous = json.loads(metadata_path.read_text(encoding="utf-8"))
        if (previous["model"] != model or previous["split"] != config["split"]
                or previous.get("thinking", "disabled") != thinking_label
                or previous.get("reasoning_effort") != config["reasoning_effort"]
                or previous.get("base_url") != base_url
                or previous.get("provider", "deepseek") != args.provider
                or previous.get("max_output_tokens") != args.max_output_tokens
                or previous.get("qwen_nonthinking", False) != args.qwen_nonthinking):
            raise SystemExit("This run directory belongs to a different model or thinking configuration")
    else:
        metadata_path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")

    api_timeout = args.api_timeout or (300 if args.thinking == "enabled" else 90)
    client = OpenAI(api_key=key, base_url=base_url, timeout=api_timeout, max_retries=2)
    pending = []
    for question in questions[:args.limit]:
        qid = int(question["question_id"])
        if qid in existing and not existing[qid].get("api_error"):
            continue
        pending.append(question)
    if args.max_new is not None:
        pending = pending[:args.max_new]

    def call_one(question: dict) -> dict:
        qid = int(question["question_id"])
        record = {"question_id": qid, "db_id": question["db_id"], "model": model,
                  "timestamp_beijing": datetime.now(BEIJING).isoformat()}
        try:
            request = {"model": model,
                       "messages": [{"role": "user", "content": question["prompt"]}]}
            if args.max_output_tokens is not None:
                request["max_tokens"] = args.max_output_tokens
            if args.provider == "deepseek":
                request["extra_body"] = {"thinking": {"type": args.thinking}}
            if args.qwen_nonthinking:
                request["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
            if args.provider == "deepseek" and args.thinking == "enabled":
                request["reasoning_effort"] = args.reasoning_effort
            response = client.chat.completions.create(**request)
            content = response.choices[0].message.content or ""
            record["raw_response"] = content
            record["predicted_sql"] = extract_sql(content)
            usage = response.usage
            record["input_tokens"] = usage.prompt_tokens if usage else 0
            record["output_tokens"] = usage.completion_tokens if usage else 0
            details = getattr(usage, "completion_tokens_details", None) if usage else None
            record["reasoning_tokens"] = getattr(details, "reasoning_tokens", None) if details else None
            record["api_model"] = response.model
            record["finish_reason"] = response.choices[0].finish_reason
        except Exception as exc:
            record["api_error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
            record["predicted_sql"] = ""
            record["input_tokens"] = 0
            record["output_tokens"] = 0
        if args.delay > 0:
            time.sleep(args.delay)
        return record

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(call_one, question) for question in pending]
        for future in as_completed(futures):
            record = future.result()
            qid = int(record["question_id"])
            append_jsonl(predictions, record)
            existing[qid] = record
            print(f"[{len(existing)}/{args.limit}] question {qid} {record['db_id']}: "
                  f"{'API error' if record.get('api_error') else 'saved'}", flush=True)
    report(args)


def chart_html(figure, include_js: bool) -> str:
    from plotly.offline import plot
    return plot(figure, include_plotlyjs=include_js, output_type="div", config={"displayModeBar": False})


def make_dashboard(results: list[dict], summary: dict, run_dir: Path) -> None:
    import plotly.graph_objects as go

    by_diff = defaultdict(lambda: [0, 0])
    by_db = defaultdict(lambda: [0, 0])
    for result in results:
        for bucket, key in ((by_diff, result["difficulty"]), (by_db, result["db_id"])):
            bucket[key][0] += int(result["correct"])
            bucket[key][1] += 1
    diff_order = [name for name in ("simple", "moderate", "challenging") if name in by_diff]
    difficulty = go.Figure(go.Bar(
        x=diff_order,
        y=[100 * by_diff[name][0] / by_diff[name][1] for name in diff_order],
        text=[f"{by_diff[name][0]}/{by_diff[name][1]}" for name in diff_order],
        textposition="auto", marker_color="#2563eb"))
    difficulty.update_layout(title="按难度的执行准确率", yaxis_title="EX (%)", yaxis_range=[0, 100],
                             template="plotly_white", height=350)
    db_names = sorted(by_db, key=lambda name: by_db[name][1], reverse=True)
    databases = go.Figure(go.Bar(
        x=db_names,
        y=[100 * by_db[name][0] / by_db[name][1] for name in db_names],
        text=[f"{by_db[name][0]}/{by_db[name][1]}" for name in db_names],
        textposition="auto", marker_color="#0d9488"))
    databases.update_layout(title="按数据库的执行准确率", yaxis_title="EX (%)", yaxis_range=[0, 100],
                            template="plotly_white", height=400, xaxis_tickangle=-30)
    statuses = Counter(item["status"] for item in results)
    judgeable = len(results) - statuses.get("gold_error", 0)
    judgeable_percent = 100 * sum(bool(item["correct"]) for item in results) / judgeable if judgeable else 0
    status_chart = go.Figure(go.Pie(labels=list(statuses), values=list(statuses.values()), hole=0.55))
    status_chart.update_layout(title="结果分布", template="plotly_white", height=350)

    rows = []
    for item in results:
        qid = item["question_id"]
        def shown(value) -> str:
            return html.escape(json.dumps(value, ensure_ascii=False, indent=2, default=str))

        def sample_text(key: str) -> str:
            return shown(item[key]) if key in item else "本次复用既有正确评分，未缓存结果样例"

        diagnosis = ""
        if item["status"] == "sql_error":
            diagnosis = "预测 SQL 无法执行"
        elif item["status"] == "gold_error":
            diagnosis = "参考 SQL 在当前环境执行失败，需先排查评分环境"
        elif item["status"] == "incorrect":
            predicted_sample = item.get("predicted_sample", [])
            gold_sample = item.get("gold_sample", [])
            if (item.get("predicted_rows") == item.get("gold_rows") == 1
                    and len(predicted_sample) == len(gold_sample) == 1
                    and len(predicted_sample[0]) == len(gold_sample[0])
                    and all(isinstance(a, (int, float)) and isinstance(b, (int, float))
                            and math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-12)
                            for a, b in zip(predicted_sample[0], gold_sample[0]))):
                diagnosis = "仅有微小数值差异；严格 EX 仍判错"
            elif item.get("predicted_rows") != item.get("gold_rows"):
                diagnosis = "返回行数不同"
            else:
                diagnosis = "返回值或行内容不同"
        detail = (
            f"<details><summary>查看参考答案与结果</summary>"
            f"<p><strong>差异提示：</strong>{html.escape(diagnosis)}</p>"
            f"<p><strong>题目补充说明：</strong>{html.escape(item.get('evidence', '') or '无')}</p>"
            f"<h4>预测 SQL</h4><pre>{html.escape(item.get('predicted_sql', ''))}</pre>"
            f"<h4>参考 SQL</h4><pre>{html.escape(item.get('gold_sql', ''))}</pre>"
            f"<p>预测返回 {item.get('predicted_rows', '未执行')} 行；参考返回 {item.get('gold_rows', '未执行')} 行。"
            f"每组最多展示 5 行。</p>"
            f"<h4>预测结果样例</h4><pre>{sample_text('predicted_sample')}</pre>"
            f"<h4>参考结果样例</h4><pre>{sample_text('gold_sample')}</pre>"
        )
        if item.get("status") == "incorrect":
            detail += (
                f"<p>仅预测结果中有 {item.get('only_predicted_rows', '?')} 行；"
                f"仅参考结果中有 {item.get('only_gold_rows', '?')} 行。</p>"
                f"<h4>仅预测中的行（样例）</h4><pre>{shown(item.get('only_predicted_sample', []))}</pre>"
                f"<h4>仅参考中的行（样例）</h4><pre>{shown(item.get('only_gold_sample', []))}</pre>"
            )
        if item.get("error"):
            detail += f"<h4>执行错误</h4><pre>{html.escape(item['error'])}</pre>"
        if item.get("verification_note"):
            detail += f"<h4>复核说明</h4><p>{html.escape(item['verification_note'])}</p>"
        if item.get("reference_fix_note"):
            detail += f"<h4>本地参考 SQL 兼容性复核</h4><p>{html.escape(item['reference_fix_note'])}</p>"
            if item.get("reference_execution_sql"):
                detail += (f"<h4>等价执行版本（原参考 SQL 保留在上方）</h4>"
                           f"<pre>{html.escape(item['reference_execution_sql'])}</pre>")
        detail += "</details>"
        cells = [str(qid), item["db_id"], item["difficulty"], item["status"], item["question"]]
        rows.append("<tr id='q-{}' data-status='{}' data-db='{}' data-difficulty='{}'>{}</tr>".format(
            qid,
            html.escape(item["status"], quote=True),
            html.escape(item["db_id"], quote=True),
            html.escape(item["difficulty"], quote=True),
            "".join(f"<td>{html.escape(value)}</td>" for value in cells) + f"<td>{detail}</td>"))
    status_options = "".join(f"<option>{html.escape(name)}</option>" for name in sorted(statuses))
    db_options = "".join(f"<option>{html.escape(name)}</option>" for name in sorted(by_db))
    diff_options = "".join(f"<option>{html.escape(name)}</option>" for name in diff_order)
    figures = (chart_html(difficulty, True) + chart_html(databases, False) +
               chart_html(status_chart, False))
    document = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>BIRD × {html.escape(summary['model'])} 评测</title><style>
body{{font:15px system-ui, sans-serif;background:#f5f7fb;color:#172033;margin:0;padding:28px}}
main{{max-width:1450px;margin:auto}}h1{{margin:0 0 8px}}.muted{{color:#64748b}}
.cards{{display:flex;flex-wrap:wrap;gap:12px;margin:22px 0}}.card{{background:white;padding:16px 22px;border-radius:12px;min-width:170px;box-shadow:0 2px 12px #dbe3ef}}
.card strong{{display:block;font-size:27px;margin-top:4px}}.charts{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}
.charts>div{{background:white;border-radius:12px;padding:8px}}.charts>div:nth-child(2){{grid-column:span 2}}
.panel{{background:white;border-radius:12px;padding:16px;margin-top:16px;overflow:auto}}
select,input{{padding:8px;border:1px solid #cbd5e1;border-radius:6px;margin:0 8px 12px 0}}
table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{border-bottom:1px solid #e2e8f0;padding:9px;text-align:left;vertical-align:top}}
th{{position:sticky;top:0;background:white}}td:nth-child(5){{min-width:260px;max-width:450px;white-space:pre-wrap;overflow-wrap:anywhere}}
td:nth-child(6){{min-width:440px;max-width:800px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#f1f5f9;padding:10px;border-radius:6px}}
details summary{{cursor:pointer;color:#1d4ed8;font-weight:600}}h4{{margin:14px 0 6px}}
@media(max-width:850px){{.charts{{display:block}}}}
</style></head><body><main><h1>{html.escape(summary['dataset'])} × {html.escape(summary['model'])}</h1>
<div class="muted">{html.escape(summary['model'])} · 思考模式：{html.escape(summary.get('thinking', 'unknown'))} · 生成时间（北京时间）{html.escape(summary['generated_beijing'])}</div>
<div class="cards"><div class="card">执行准确率 EX<strong>{summary['ex_percent']:.2f}%</strong></div>
<div class="card">参考可执行题正确率<strong>{judgeable_percent:.2f}%</strong><small>{judgeable}/{len(results)} 题可判定</small></div>
<div class="card">覆盖题数<strong>{summary['evaluated']}/{summary['total']}</strong></div>
<div class="card">正确题数<strong>{summary['correct']}</strong></div>
<div class="card">输入 tokens<strong>{summary['input_tokens']:,}</strong></div>
<div class="card">输出 tokens<strong>{summary['output_tokens']:,}</strong></div></div>
<p class="muted">EX 分母为已请求题数；API/SQL 错误计为错误。参考 SQL 超时或执行失败单列为参考异常，不归因模型；“参考可执行题正确率”仅在可判定题上计算。未请求的题不进入分母。完整集成绩需覆盖 {summary['total']}/{summary['total']}。结果按去重后的行集合比较；下方样例最多展示 5 行，完整 SQL 与原始响应见同目录 JSONL。</p>
<div class="charts">{figures}</div><div class="panel"><h2>逐题结果</h2>
<input id="search" placeholder="搜索题目或 SQL"><select id="status"><option value="">全部状态</option>{status_options}</select>
<select id="db"><option value="">全部数据库</option>{db_options}</select>
<select id="difficulty"><option value="">全部难度</option>{diff_options}</select>
<table><thead><tr><th>ID</th><th>数据库</th><th>难度</th><th>状态</th><th>问题</th><th>答案与结果对照</th></tr></thead><tbody id="rows">{''.join(rows)}</tbody></table></div>
<script>for(const id of ['search','status','db','difficulty']){{document.getElementById(id).addEventListener('input',()=>{{
const q=document.getElementById('search').value.toLowerCase(),s=document.getElementById('status').value,d=document.getElementById('db').value,f=document.getElementById('difficulty').value;
for(const row of document.querySelectorAll('#rows tr'))row.hidden=!!((s&&row.dataset.status!==s)||(d&&row.dataset.db!==d)||(f&&row.dataset.difficulty!==f)||(q&&!row.textContent.toLowerCase().includes(q)));
}})}};</script></main></body></html>"""
    (run_dir / "report.html").write_text(document, encoding="utf-8")


def report(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    config_path = run_dir / "run_config.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    select_dataset(dataset_key_from_config(config))
    raw = latest_records(run_dir / "predictions.jsonl")
    if not raw:
        raise SystemExit(f"No predictions found in {run_dir}")
    questions = {int(q["question_id"]): q for q in load_jsonl(PROMPTS)}
    results = []
    pending_ids = [qid for qid in sorted(raw) if qid in questions]
    scored_path = run_dir / "scored.jsonl"
    partial_path = run_dir / "scored.partial.jsonl"
    previous = latest_records(scored_path) if scored_path.exists() else {}
    if partial_path.exists():
        previous.update(latest_records(partial_path))
    reused = 0
    print(f"Scoring {len(pending_ids)} saved SQL predictions (no API calls)...", flush=True)
    def score_one(qid: int) -> tuple[int, dict, bool]:
        old = previous.get(qid)
        if (not getattr(args, "rescore_all", False) and old
                and old.get("status") == "correct"
                and old.get("predicted_sql") == raw[qid].get("predicted_sql")):
            result = dict(old)
            result["gold_sql"] = questions[qid]["SQL"]
            result["evidence"] = questions[qid].get("evidence", "")
            return qid, result, True
        else:
            result = score_record(raw[qid], questions[qid], args.sql_timeout)
            return qid, result, False

    by_id = {}
    workers = min(5, len(pending_ids))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(score_one, qid) for qid in pending_ids]
        for index, future in enumerate(as_completed(futures), 1):
            qid, result, was_reused = future.result()
            by_id[qid] = result
            reused += int(was_reused)
            with partial_path.open("a", encoding="utf-8") as partial_output:
                partial_output.write(json.dumps(result, ensure_ascii=False, default=str) + "\n")
            print(f"[EX {index}/{len(pending_ids)}] question {qid}: {result['status']}", flush=True)
    results = [by_id[qid] for qid in pending_ids]
    correct = sum(int(item["correct"]) for item in results)
    model = config.get("model", results[0].get("model", "unknown"))
    summary = {"dataset": ACTIVE_DATASET["name"], "model": model,
               "thinking": config.get("thinking", "unknown"),
               "reasoning_effort": config.get("reasoning_effort"),
               "generated_beijing": datetime.now(BEIJING).isoformat(),
               "total": len(questions), "evaluated": len(results), "correct": correct,
               "reused_correct_scores": reused, "sql_timeout_seconds": args.sql_timeout,
               "ex_percent": 100 * correct / len(results),
               "status_counts": dict(Counter(item["status"] for item in results)),
               "input_tokens": sum(item.get("input_tokens", 0) for item in results),
               "output_tokens": sum(item.get("output_tokens", 0) for item in results),
               "reasoning_tokens_reported": sum(item.get("reasoning_tokens") or 0 for item in results)}
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "scored.jsonl").write_text("".join(json.dumps(item, ensure_ascii=False, default=str) + "\n" for item in results), encoding="utf-8")
    partial_path.unlink(missing_ok=True)
    official_pred = {str(qid): f"{raw[qid].get('predicted_sql', '')}\t----- bird -----\t{questions[qid]['db_id']}"
                     for qid in range(len(questions)) if qid in raw}
    (run_dir / "official_predictions.json").write_text(json.dumps(official_pred, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "gold.sql").write_text("".join(f"{questions[qid]['SQL']}\t{questions[qid]['db_id']}\n"
                                            for qid in range(len(questions))), encoding="utf-8")
    make_dashboard(results, summary, run_dir)
    print(f"EX {summary['ex_percent']:.2f}% ({correct}/{len(results)}); report: {run_dir / 'report.html'}")


def render(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    config = json.loads((run_dir / "run_config.json").read_text(encoding="utf-8"))
    select_dataset(dataset_key_from_config(config))
    scored = load_jsonl(run_dir / "scored.jsonl")
    questions = {int(q["question_id"]): q for q in load_jsonl(PROMPTS)}
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    for item in scored:
        question = questions[int(item["question_id"])]
        item["gold_sql"] = question["SQL"]
        item["evidence"] = question.get("evidence", "")
    make_dashboard(scored, summary, run_dir)
    print(f"Rendered {len(scored)} rows: {run_dir / 'report.html'}")


def compare(args: argparse.Namespace) -> None:
    import plotly.graph_objects as go

    old_label, new_label = args.baseline_label, args.target_label
    gain_label, loss_label = f"仅{new_label}正确", f"仅{old_label}正确"
    baseline_dir = Path(args.baseline_dir).resolve()
    thinking_dir = Path(args.thinking_dir).resolve()
    output = Path(args.output).resolve()
    old_report = Path(os.path.relpath(baseline_dir / "report.html", output.parent)).as_posix()
    new_report = Path(os.path.relpath(thinking_dir / "report.html", output.parent)).as_posix()
    base = latest_records(baseline_dir / "scored.jsonl")
    thought = latest_records(thinking_dir / "scored.jsonl")
    ids = sorted(set(base) & set(thought))
    if not ids:
        raise SystemExit("No overlapping scored questions")
    base_summary = json.loads((baseline_dir / "summary.json").read_text(encoding="utf-8"))
    thought_summary = json.loads((thinking_dir / "summary.json").read_text(encoding="utf-8"))
    by_db = defaultdict(lambda: [0, 0, 0])
    transitions = Counter()
    rows = []

    def side_by_side_part(record: dict, label: str) -> str:
        def sample(key: str) -> str:
            return html.escape(json.dumps(record.get(key, []), ensure_ascii=False, indent=2, default=str))

        error = record.get("predicted_error") or record.get("gold_error") or record.get("error")
        error_html = f"<p class='error'>执行错误：{html.escape(str(error))}</p>" if error else ""
        differences = ""
        if not record["correct"] and ("only_predicted_sample" in record or "only_gold_sample" in record):
            differences = ("<h4>只在 AI 结果中的行（样例）</h4>"
                           f"<pre>{sample('only_predicted_sample')}</pre>"
                           "<h4>只在参考结果中的行（样例）</h4>"
                           f"<pre>{sample('only_gold_sample')}</pre>")
        return (f"<section><h3>{html.escape(label)} · {html.escape(record['status'])}</h3>"
                f"<h4>AI SQL</h4><pre>{html.escape(record.get('predicted_sql') or '无')}</pre>"
                f"<h4>参考 SQL</h4><pre>{html.escape(record.get('gold_sql') or '无')}</pre>"
                f"<p>AI 返回 {record.get('predicted_rows', '未知')} 行；参考返回 {record.get('gold_rows', '未知')} 行。以下最多展示 5 行。</p>"
                f"<h4>AI 结果样例</h4><pre>{sample('predicted_sample')}</pre>"
                f"<h4>参考结果样例</h4><pre>{sample('gold_sample')}</pre>"
                f"{differences}{error_html}</section>")

    for qid in ids:
        old, new = base[qid], thought[qid]
        transition = ("两次都正确" if old["correct"] and new["correct"] else
                      gain_label if new["correct"] else
                      loss_label if old["correct"] else "两次都错误")
        transitions[transition] += 1
        counts = by_db[old["db_id"]]
        counts[0] += 1
        counts[1] += int(old["correct"])
        counts[2] += int(new["correct"])
        old_link = f"{html.escape(old_report)}#q-{qid}"
        new_link = f"{html.escape(new_report)}#q-{qid}"
        evidence = old.get("evidence") or new.get("evidence")
        evidence_html = f"<p><b>随题证据：</b>{html.escape(str(evidence))}</p>" if evidence else ""
        same_sql = old.get("predicted_sql") == new.get("predicted_sql")
        sql_note = "两次 AI SQL 完全相同。" if same_sql else "两次 AI SQL 不同，请对照条件、关联、聚合与输出列。"
        details = ("<details><summary>在本页对照两次 SQL 和结果</summary>"
                   f"{evidence_html}<p>{sql_note}</p>"
                   f"<div class='side-by-side'>{side_by_side_part(old, old_label)}{side_by_side_part(new, new_label)}</div>"
                   "</details>")
        rows.append(f"<tr id='q-{qid}' data-transition='{html.escape(transition)}'>"
                    f"<td>{qid}</td><td>{html.escape(old['db_id'])}</td>"
                    f"<td>{html.escape(transition)}</td>"
                    f"<td><a href='{old_link}'>{html.escape(old['status'])}</a></td>"
                    f"<td><a href='{new_link}'>{html.escape(new['status'])}</a></td>"
                    f"<td>{html.escape(old['question'])}</td><td>{details}</td></tr>")
    db_names = sorted(by_db)
    fig = go.Figure()
    fig.add_bar(name=old_label, x=db_names, y=[100 * by_db[d][1] / by_db[d][0] for d in db_names])
    fig.add_bar(name=new_label, x=db_names, y=[100 * by_db[d][2] / by_db[d][0] for d in db_names])
    fig.update_layout(title="各数据库 EX 对比", barmode="group", yaxis_title="EX (%)",
                      yaxis_range=[0, 100], xaxis_tickangle=-30, template="plotly_white", height=500)
    score_old = sum(int(base[i]["correct"]) for i in ids)
    score_new = sum(int(thought[i]["correct"]) for i in ids)
    options = "".join(f"<option>{html.escape(name)}</option>" for name in
                      (gain_label, loss_label, "两次都正确", "两次都错误"))
    body = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>{html.escape(args.title)}</title>
<style>body{{font:15px system-ui,sans-serif;background:#f5f7fb;color:#172033;padding:28px}}main{{max-width:1350px;margin:auto}}
.cards{{display:flex;flex-wrap:wrap;gap:12px;margin:20px 0}}.card,.panel{{background:white;border-radius:12px;padding:18px;box-shadow:0 2px 12px #dbe3ef}}
.card{{min-width:170px}}strong{{display:block;font-size:25px}}.panel{{margin-top:16px;overflow:auto}}table{{border-collapse:collapse;width:100%}}
th,td{{padding:9px;border-bottom:1px solid #e2e8f0;text-align:left;vertical-align:top}}td:nth-child(6){{min-width:260px;max-width:430px}}td:last-child{{min-width:650px}}
select,input{{padding:8px;margin:6px;border:1px solid #cbd5e1;border-radius:6px}}a{{color:#1d4ed8}}
details summary{{cursor:pointer;color:#1d4ed8;font-weight:600}}.side-by-side{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px}}
.side-by-side section{{min-width:0;padding:12px;border:1px solid #dbe3ef;border-radius:8px;background:#f8fafc}}
.side-by-side h3{{margin:0 0 8px}}.side-by-side h4{{margin:12px 0 5px}}.side-by-side pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:white;padding:9px;border-radius:5px}}
.error{{color:#b91c1c}}@media(max-width:900px){{.side-by-side{{grid-template-columns:1fr}}}}</style></head>
<body><main><h1>{html.escape(args.title)}</h1><p>共同题目 {len(ids)} 道 · deepseek-flash · 北京时间 {html.escape(datetime.now(BEIJING).isoformat())}</p>
<div class="cards"><div class="card">{html.escape(old_label)} EX<strong>{100*score_old/len(ids):.2f}%</strong></div>
<div class="card">{html.escape(new_label)} EX<strong>{100*score_new/len(ids):.2f}%</strong></div>
<div class="card">净增正确题<strong>{score_new-score_old:+d}</strong></div>
<div class="card">{html.escape(gain_label)}<strong>{transitions[gain_label]}</strong></div>
<div class="card">{html.escape(loss_label)}<strong>{transitions[loss_label]}</strong></div></div>
<p>输入 tokens：{base_summary['input_tokens']:,} → {thought_summary['input_tokens']:,}；输出 tokens：{base_summary['output_tokens']:,} → {thought_summary['output_tokens']:,}。
{html.escape(args.note)}</p>
<div class="panel">{chart_html(fig, True)}</div><div class="panel"><h2>逐题变化</h2>
<select id="transition"><option value="">全部变化</option>{options}</select><input id="search" placeholder="搜索数据库或问题">
<p>先按“变化”筛选，再点每行的“在本页对照两次 SQL 和结果”。状态链接可打开对应运行的完整逐题报告。</p>
<table><thead><tr><th>ID</th><th>数据库</th><th>变化</th><th>{html.escape(old_label)}</th><th>{html.escape(new_label)}</th><th>问题</th><th>两次答案对照</th></tr></thead>
<tbody id="rows">{''.join(rows)}</tbody></table></div>
<script>for(const id of ['transition','search']){{document.getElementById(id).addEventListener('input',()=>{{
const t=document.getElementById('transition').value,q=document.getElementById('search').value.toLowerCase();
for(const row of document.querySelectorAll('#rows tr'))row.hidden=!!((t&&row.dataset.transition!==t)||(q&&!row.textContent.toLowerCase().includes(q)));
}})}};</script></main></body></html>"""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(body, encoding="utf-8")
    print(f"Comparison: {score_old}/{len(ids)} -> {score_new}/{len(ids)}; {output}")


def main() -> None:
    parser = argparse.ArgumentParser(description="BIRD SQLite/MySQL × DeepSeek API evaluation")
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run", help="Call API, score responses, and build dashboard")
    run_parser.add_argument("--run-dir", default=str(ROOT / "runs/deepseek_flash"))
    run_parser.add_argument("--dataset", choices=DATASETS, default="mini_sqlite")
    run_parser.add_argument("--model", default=None)
    run_parser.add_argument("--provider", choices=("deepseek", "openai-compatible"), default="deepseek")
    run_parser.add_argument("--base-url", default=None)
    run_parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    run_parser.add_argument("--max-output-tokens", type=int, default=None)
    run_parser.add_argument("--qwen-nonthinking", action="store_true", help="Use Qwen chat template with thinking disabled")
    run_parser.add_argument("--thinking", choices=("disabled", "enabled"), default="disabled")
    run_parser.add_argument("--reasoning-effort", choices=("low", "high", "max"), default="high")
    run_parser.add_argument("--limit", type=int, default=10)
    run_parser.add_argument("--max-new", type=int, default=None)
    run_parser.add_argument("--workers", type=int, default=1, help="Concurrent API requests")
    run_parser.add_argument("--delay", type=float, default=0)
    run_parser.add_argument("--api-timeout", type=float, default=None)
    run_parser.add_argument("--sql-timeout", type=float, default=60)
    report_parser = sub.add_parser("report", help="Re-score saved responses without API calls")
    report_parser.add_argument("--run-dir", default=str(ROOT / "runs/deepseek_flash"))
    report_parser.add_argument("--sql-timeout", type=float, default=60)
    report_parser.add_argument("--rescore-all", action="store_true", help="Re-evaluate even previously correct rows")
    render_parser = sub.add_parser("render", help="Rebuild HTML from scored.jsonl without SQL or API calls")
    render_parser.add_argument("--run-dir", default=str(ROOT / "runs/deepseek_flash"))
    compare_parser = sub.add_parser("compare", help="Compare two scored runs")
    compare_parser.add_argument("--baseline-dir", default=str(ROOT / "runs/deepseek_flash"))
    compare_parser.add_argument("--thinking-dir", default=str(ROOT / "runs/deepseek_flash_thinking"))
    compare_parser.add_argument("--output", default=str(ROOT / "runs/comparison_thinking_vs_disabled.html"))
    compare_parser.add_argument("--baseline-label", default="关闭思考")
    compare_parser.add_argument("--target-label", default="开启思考")
    compare_parser.add_argument("--title", default="BIRD Mini-Dev：思考模式对比")
    compare_parser.add_argument("--note", default="两次使用同一套题目、固定提示词和本地严格 EX 评分；仅改变思考模式。")
    args = parser.parse_args()
    if args.command == "run":
        run(args)
    elif args.command == "report":
        report(args)
    elif args.command == "render":
        render(args)
    else:
        compare(args)


if __name__ == "__main__":
    main()
