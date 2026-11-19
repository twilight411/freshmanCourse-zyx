"""Score saved WHU predictions on the official BEAVER MySQL dumps.

This follows the repository's EX comparator (set of rows, values as stripped
strings, column names ignored) but never treats a SQL error as an empty result.
"""

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import mysql.connector
import pandas as pd
import sqlglot
from sqlglot import exp

ROOT = Path(__file__).resolve().parent
MAX_RESULT_ROWS = 150_000
sys.path.insert(0, str(ROOT / "eval"))
from utils.ex_acc import compare_results, normalize_dataframe_values  # noqa: E402

BEIJING = timezone(timedelta(hours=8))
SPLITS = ("dw", "dw_real", "neutron", "nova")
MODELS = ("deepseek", "qwen", "gemini")


def now():
    return datetime.now(BEIJING).isoformat(timespec="seconds")


def password():
    for line in Path(r"D:\Bird-Benchmark\mysql_runtime\.env").read_text(encoding="utf-8-sig").splitlines():
        if line.startswith("BIRD_MYSQL_ROOT_PASSWORD="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("MySQL password not found")


def read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def safe_sql(sql):
    """Allow one read-only query; leave SQL syntax validation to MySQL itself."""
    parts, current = [], []
    i, state = 0, "normal"
    sql = sql or ""
    while i < len(sql):
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < len(sql) else ""
        if state == "normal":
            if ch in ("'", '"', "`"):
                state = ch
                current.append(ch)
            elif ch == "-" and nxt == "-" or ch == "#":
                state = "line_comment"
                i += 1 if ch == "-" else 0
                current.append(" ")
            elif ch == "/" and nxt == "*":
                state = "block_comment"
                i += 1
                current.append(" ")
            elif ch == ";":
                parts.append("".join(current).strip())
                current = []
            else:
                current.append(ch)
        elif state in ("'", '"', "`"):
            current.append(ch)
            if ch == "\\" and nxt:
                current.append(nxt)
                i += 1
            elif ch == state:
                if nxt == state:
                    current.append(nxt)
                    i += 1
                else:
                    state = "normal"
        elif state == "line_comment":
            if ch == "\n":
                current.append("\n")
                state = "normal"
        elif state == "block_comment":
            if ch == "*" and nxt == "/":
                i += 1
                state = "normal"
        i += 1
    parts.append("".join(current).strip())
    parts = [part for part in parts if part]
    if len(parts) != 1 or state in ("'", '"', "`", "block_comment"):
        return False
    first = re.search(r"[A-Za-z_]+", parts[0])
    return bool(first and first.group().upper() in ("SELECT", "WITH"))


def execute(sql, db, secret):
    if not sql:
        return {"error": "Empty SQL", "row_count": None, "columns": [], "sample": [], "elapsed_ms": None}, None
    if not safe_sql(sql):
        return {"error": "SQL must contain exactly one complete read-only SELECT/CTE statement", "row_count": None,
                "columns": [], "sample": [], "elapsed_ms": None}, None
    started = time.perf_counter()
    try:
        conn = mysql.connector.connect(host="127.0.0.1", port=33306, user="root", password=secret,
                                       database=db, connection_timeout=10, read_timeout=20,
                                       consume_results=True)
        try:
            cursor = conn.cursor()
            cursor.execute("SET SESSION MAX_EXECUTION_TIME=10000")
            cursor.execute(sql)
            columns = [col[0] for col in (cursor.description or [])]
            rows = []
            while chunk := cursor.fetchmany(2000):
                rows.extend(chunk)
                if len(rows) > MAX_RESULT_ROWS:
                    return {"error": f"Result exceeds {MAX_RESULT_ROWS} rows; omitted from EX to limit memory",
                            "row_count": None, "columns": columns,
                            "sample": [[str(x) if x is not None else None for x in row] for row in rows[:5]],
                            "elapsed_ms": round((time.perf_counter()-started)*1000, 2)}, None
            frame = pd.DataFrame(rows, columns=columns)
            sample = [[str(x) if x is not None else None for x in row] for row in rows[:5]]
            return {"error": None, "row_count": len(rows), "columns": columns,
                    "sample": sample, "elapsed_ms": round((time.perf_counter()-started)*1000, 2)}, frame
        finally:
            conn.close()
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {str(exc)[:500]}", "row_count": None,
                "columns": [], "sample": [], "elapsed_ms": round((time.perf_counter()-started)*1000, 2)}, None


def classify(pred_sql, pred, gold):
    if pred["error"]:
        return "SQL 执行错误" if pred_sql else "模型未生成 SQL"
    if pred["row_count"] == 0 and gold["row_count"] != 0:
        return "筛选条件或连接导致空结果（待人工核查）"
    if len(pred["columns"]) != len(gold["columns"]):
        return "输出字段数不同"
    if pred["row_count"] != gold["row_count"]:
        return "输出行数不同（待人工核查）"
    return "结果值不同（待人工核查）"


def diagnose(pred_sql, gold_sql, pred, gold):
    notes = []
    if pred["error"]:
        notes.append("AI SQL 执行失败：" + pred["error"])
    if pred["row_count"] is not None and gold["row_count"] is not None:
        notes.append(f"AI 返回 {pred['row_count']} 行，标准返回 {gold['row_count']} 行。")
        if len(pred["columns"]) != len(gold["columns"]):
            notes.append(f"AI 输出 {len(pred['columns'])} 列，标准输出 {len(gold['columns'])} 列。")
    try:
        ai = sqlglot.parse_one(pred_sql, read="mysql")
        ref = sqlglot.parse_one(gold_sql, read="mysql")
        if ai is None or ref is None:
            return notes
        def tables(tree):
            ctes = {x.alias_or_name.lower() for x in tree.find_all(exp.CTE)}
            return {x.name.lower() for x in tree.find_all(exp.Table) if x.name.lower() not in ctes}
        ai_tables, ref_tables = tables(ai), tables(ref)
        missing, extra = sorted(ref_tables-ai_tables), sorted(ai_tables-ref_tables)
        if missing:
            notes.append("标准 SQL 使用而 AI 未使用的表：" + ", ".join(missing[:8]))
        if extra:
            notes.append("AI 额外使用的表：" + ", ".join(extra[:8]))
        def columns(tree):
            return {x.name.lower() for x in tree.find_all(exp.Column)}
        ai_cols, ref_cols = columns(ai), columns(ref)
        missing_cols = sorted(ref_cols-ai_cols)
        if missing_cols:
            notes.append("标准 SQL 使用而 AI 未使用的字段：" + ", ".join(missing_cols[:10]))
        def literals(tree):
            return {str(x.this) for x in tree.find_all(exp.Literal) if x.is_string}
        ai_lit, ref_lit = literals(ai), literals(ref)
        if ai_lit != ref_lit:
            notes.append("字符串取值不同；AI 独有：" + repr(sorted(ai_lit-ref_lit)[:6]) +
                         "；标准独有：" + repr(sorted(ref_lit-ai_lit)[:6]))
        for node, label in ((exp.Where, "WHERE 筛选"), (exp.Group, "GROUP BY 分组"),
                            (exp.Having, "HAVING 筛选"), (exp.Order, "ORDER BY 排序"),
                            (exp.Limit, "LIMIT 限制")):
            a, b = bool(ai.find(node)), bool(ref.find(node))
            if a != b:
                notes.append(f"{label}：AI {'有' if a else '没有'}，标准 {'有' if b else '没有'}。")
        def functions(tree):
            return {type(x).__name__.lower() for x in tree.find_all(exp.Func)}
        ai_func, ref_func = functions(ai), functions(ref)
        missing_funcs = sorted(ref_func-ai_func)
        if missing_funcs:
            notes.append("标准 SQL 使用而 AI 未使用的函数或聚合：" + ", ".join(missing_funcs[:8]))
    except Exception as exc:
        notes.append("SQL 结构分析未成功：" + str(exc)[:150])
    notes.append("以上是 SQL 结构和结果差异线索，具体因果关系需人工结合题目与表数据核查。")
    return notes


def difference(pred_df, gold_df):
    if pred_df is None or gold_df is None:
        return None
    if len(pred_df) > 50_000 or len(gold_df) > 50_000:
        return {"note": "至少一侧超过 5 万行；为控制内存，仅展示各自前 5 行与总行数。"}
    if len(pred_df.columns) != len(gold_df.columns):
        return {"note": "两侧列数不同，无法逐行计算集合差异。"}
    ai = {tuple(str(value) for value in row) for row in normalize_dataframe_values(pred_df)}
    ref = {tuple(str(value) for value in row) for row in normalize_dataframe_values(gold_df)}
    return {"ai_only_count": len(ai-ref), "gold_only_count": len(ref-ai),
            "ai_only_sample": [list(x) for x in sorted(ai-ref)[:5]],
            "gold_only_sample": [list(x) for x in sorted(ref-ai)[:5]]}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--split", choices=(*SPLITS, "all"), default="all")
    p.add_argument("--model", choices=(*MODELS, "both", "all"), default="all")
    args = p.parse_args()
    secret = password()
    splits = SPLITS if args.split == "all" else (args.split,)
    models = MODELS if args.model in ("both", "all") else (args.model,)
    for split in splits:
        questions = json.loads((ROOT / "data" / split / "dev_sampled.json").read_text(encoding="utf-8"))
        db = "dw" if split == "dw_real" else split
        cache_path = ROOT / "runs" / f"{split}_gold_results.json"
        cached = {x["id"]: x for x in json.loads(cache_path.read_text(encoding="utf-8"))} if cache_path.exists() else {}
        model_runs = {}
        for model in models:
            run = ROOT / "runs" / f"{split}_{model}"
            preds = {str(x["id"]): x for x in read_jsonl(run / "predictions.jsonl")}
            prompts = {str(x["id"]): x for x in read_jsonl(run / "prompts.jsonl")}
            scores_path = run / "scores.json"
            scores = {x["id"]: x for x in json.loads(scores_path.read_text(encoding="utf-8"))} if scores_path.exists() else {}
            model_runs[model] = (run, preds, prompts, scores_path, scores)
        for q in questions:
            qid = str(q["id"])
            pending = []
            for model in models:
                run, preds, prompts, scores_path, scores = model_runs[model]
                pred = preds.get(qid)
                if not pred:
                    continue
                sql_hash = hashlib.sha256(pred.get("predicted_sql", "").encode("utf-8")).hexdigest()
                if (qid in scores and scores[qid].get("sql_sha256") == sql_hash
                        and scores[qid].get("score_version") == 3 and scores[qid]["status"] != "gold_error"
                        and not (scores[qid].get("ai_result", {}).get("error") or "").startswith("Only a single")):
                    continue
                pending.append((model, pred, sql_hash))
            if not pending and qid in cached:
                continue
            gold, gold_df = execute(q["sql"], db, secret)
            if qid not in cached or cached[qid]["error"] != gold["error"]:
                cached[qid] = {"id": qid, **gold}
                cache_path.write_text(json.dumps(list(cached.values()), ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[{now()}] {split} gold {qid}: {'error' if gold['error'] else 'ok'}", flush=True)
            for model, pred, sql_hash in pending:
                run, preds, prompts, scores_path, scores = model_runs[model]
                selected_tables = set(prompts.get(qid, {}).get("selected_tables") or [])
                missing_tables = sorted(set(q.get("tables") or []) - selected_tables)
                result, pred_df = execute(pred.get("predicted_sql", ""), db, secret)
                if gold["error"]:
                    status, detail = "gold_error", "Gold SQL failed; this question cannot be scored"
                elif result["error"]:
                    status, detail = "wrong", result["error"]
                else:
                    if gold_df is None:
                        status, detail = "gold_error", "Gold SQL could not be compared"
                    else:
                        matched, detail = compare_results(pred_df, gold_df)
                        status = "correct" if matched else "wrong"
                scores[qid] = {"id": qid, "status": status, "comparison": detail,
                               "score_version": 3, "sql_sha256": sql_hash,
                               "retrieval_missing_tables": missing_tables,
                               "error_type": ("模型未看到参考所需表（检索缺失）" if missing_tables else
                                              classify(pred.get("predicted_sql", ""), result, gold)) if status == "wrong" else None,
                               "diagnosis": (["参考 SQL 使用、但提示词未提供的表：" + ", ".join(missing_tables) + "。这是检索上下文缺失，不能仅归因于模型生成。"] if missing_tables else [])
                               + (diagnose(pred.get("predicted_sql", ""), q["sql"], result, gold) if status == "wrong" else []),
                               "difference": difference(pred_df, gold_df) if status == "wrong" and not result["error"] and not gold["error"] else None,
                               "ai_result": result, "gold_result": gold, "scored_beijing": now()}
                scores_path.write_text(json.dumps(list(scores.values()), ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"[{now()}] {split}/{model} {len(scores)}/{len(questions)} {qid}: {status}", flush=True)
            del gold_df
        for model in models:
            run, preds, prompts, scores_path, scores = model_runs[model]
            counts = {k: sum(v["status"] == k for v in scores.values()) for k in ("correct", "wrong", "gold_error")}
            retrieval_missing = sum(bool(set(q.get("tables") or []) -
                                         set(prompts.get(str(q["id"]), {}).get("selected_tables") or []))
                                    for q in questions)
            (run / "summary.json").write_text(json.dumps({"model": model, "split": split,
                "sample_count": len(questions), "scored": len(scores), "counts": counts,
                "retrieval_missing_count": retrieval_missing,
                "ex_percent": round(100*counts["correct"]/(counts["correct"]+counts["wrong"]), 2)
                if counts["correct"]+counts["wrong"] else None, "generated_beijing": now()},
                ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
