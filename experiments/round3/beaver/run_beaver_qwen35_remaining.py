"""Run qwen3.5-flash on BEAVER questions without an old Qwen answer."""
from __future__ import annotations

import html
import json
import socket
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

import run_beaver_full as base

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "beaver_full_run"
CONF = base.read(ROOT / ".beaver_full/thingo_secrets.json")
MODEL = "qwen3.5-flash"
SPLITS = base.SPLITS
KEY = CONF["keys"]["qwen"]
STATUS = OUT / "qwen35_status.json"
LEDGER = OUT / "qwen35_usage.jsonl"


def target(split):
    path = OUT / f"{split}_qwen35"
    path.mkdir(exist_ok=True)
    return path


def saved(split, filename):
    return {str(x["id"]): x for x in base.jl(target(split) / filename)}


predictions = {split: saved(split, "predictions.jsonl") for split in SPLITS}
scores = {split: saved(split, "scores.jsonl") for split in SPLITS}
prompts = {split: saved(split, "prompts.jsonl") for split in SPLITS}


def score(split, question, prediction):
    qid = str(question["id"])
    if qid in scores[split] or not prediction.get("predicted_sql"):
        return
    password = base.scorer.password()
    db = "dw" if split == "dw_real" else split
    gold, gold_df = base.scorer.execute(question["sql"], db, password)
    if base.execution_issue(gold.get("error")) == "database_connection":
        raise RuntimeError("Private evaluation MySQL disconnected while running reference SQL; saved answer must be replayed")
    result, pred_df = base.scorer.execute(prediction["predicted_sql"], db, password)
    if base.execution_issue(result.get("error")) == "database_connection":
        raise RuntimeError("Private evaluation MySQL disconnected while running model SQL; saved answer must be replayed")
    if gold.get("error"):
        state, comparison = "gold_error", "Gold SQL failed"
    elif base.execution_issue(result.get("error")):
        state, comparison = "execution_unresolved", result["error"]
    elif result.get("error"):
        state, comparison = "wrong", result["error"]
    else:
        matched, comparison = base.scorer.compare_results(pred_df, gold_df)
        state = "correct" if matched else "wrong"
    selected = prompts[split].get(qid, {}).get("selected_tables") or []
    missing = sorted(set(question.get("tables") or []) - set(selected))
    row = {"id": qid, "status": state, "comparison": comparison,
           "retrieval_missing_tables": missing, "ai_result": result,
           "gold_result": gold, "scored_beijing": base.now(),
           "model": MODEL, "provider": "thingo"}
    base.append(target(split) / "scores.jsonl", row)
    scores[split][qid] = row


def snapshot(state, message="", current=None):
    runs = []
    for split in SPLITS:
        old_ids = {qid for qid, row in base.predictions[split, "qwen"].items()
                   if row.get("predicted_sql") and not row.get("api_error") and not row.get("whu_qwen_resume")}
        target_count = len(base.datasets[split]) - len(old_ids)
        counts = dict(Counter(row["status"] for row in scores[split].values()))
        runs.append({"split": split, "target": target_count,
                     "predictions": len(predictions[split]), "scored": len(scores[split]),
                     "counts": counts, "old_qwen_answers_skipped": len(old_ids)})
    data = {"provider": "thingo", "model": MODEL, "state": state, "message": message,
            "updated_beijing": base.now(), "runs": runs, "current": current,
            "usage_is_platform_balance": False,
            "tokens": {"input": sum(r.get("input_tokens", 0) for r in base.jl(LEDGER)),
                       "output": sum(r.get("output_tokens", 0) for r in base.jl(LEDGER))}}
    base.save(STATUS, data)
    cards = "".join(f'<tr><td>{html.escape(r["split"])}</td><td>{r["scored"]}/{r["target"]}</td><td>{r["old_qwen_answers_skipped"]}</td><td>{html.escape(str(r["counts"]))}</td></tr>' for r in runs)
    page = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta http-equiv="refresh" content="5"><title>Qwen 3.5 BEAVER 续跑</title><style>body{{font:16px/1.6 system-ui,"Microsoft YaHei";background:#f4f7fb;color:#17324c;max-width:950px;margin:30px auto;padding:0 15px}}main{{background:white;padding:25px;border-radius:12px}}table{{border-collapse:collapse;width:100%}}td,th{{padding:10px;border-bottom:1px solid #ddd;text-align:left}}.task{{padding:12px;background:#eaf4fb;border-radius:8px}}</style><main><h1>BEAVER · Qwen 3.5 Flash 独立续跑</h1><p>状态：{html.escape(state)} · 北京时间 {html.escape(data["updated_beijing"])} · 每 5 秒刷新</p><p>只处理原 qwen-flash 尚无有效答案的题；两种型号的评分和账本分开保存。平台余额未知，本地 token 数不是余额。</p><p>{html.escape(message)}</p><div class="task">当前题目：{html.escape(str(current or "无"))}</div><table><tr><th>数据组</th><th>Qwen 3.5 已评分/续跑目标</th><th>原 Qwen 已有答案跳过</th><th>结果</th></tr>{cards}</table><p>本轨道 token：输入 {data["tokens"]["input"]:,}，输出 {data["tokens"]["output"]:,}</p></main></html>'''
    (OUT / "qwen35_progress.html").write_text(page, encoding="utf-8")


def request(prompt):
    body = {"model": MODEL, "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 4096, "chat_template_kwargs": {"enable_thinking": False}}
    req = urllib.request.Request(CONF["endpoint"], data=json.dumps(body).encode("utf-8"),
                                 headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as response:
            data = json.load(response)
        choice = data["choices"][0]
        text = choice["message"].get("content") or ""
        usage = data.get("usage") or {}
        return {"raw_response": text, "predicted_sql": base.gen.extract_sql(text),
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "finish_reason": choice.get("finish_reason")}
    except Exception as exc:
        reason = base.safe_error(exc).replace(KEY, "[redacted]")
        if isinstance(exc, urllib.error.HTTPError):
            reason += " " + base.safe_error(exc.read(4000).decode("utf-8", "replace")).replace(KEY, "[redacted]")
        return {"api_error": reason, "http_status": getattr(exc, "code", None),
                "predicted_sql": "", "input_tokens": 0, "output_tokens": 0}


def main():
    import mysql.connector
    with mysql.connector.connect(host="127.0.0.1", port=33306, user="root",
                                 password=base.scorer.password(), connection_timeout=10):
        pass
    for split in SPLITS:
        for question in base.datasets[split]:
            pred = predictions[split].get(str(question["id"]))
            if pred and not pred.get("api_error"):
                score(split, question, pred)
    queue = []
    for split in SPLITS:
        for question in base.datasets[split]:
            qid = str(question["id"])
            old = base.predictions[split, "qwen"].get(qid)
            new = predictions[split].get(qid)
            if old and old.get("predicted_sql") and not old.get("api_error") and not old.get("whu_qwen_resume"):
                continue
            if new and new.get("predicted_sql") and not new.get("api_error"):
                continue
            queue.append((split, question))
    snapshot("running", f"Remaining {len(queue)} Qwen 3.5 model-question tasks")
    failures = 0
    max_new = int(sys.argv[sys.argv.index("--max-new-requests") + 1]) if "--max-new-requests" in sys.argv else 0
    requested = 0
    for split, question in queue:
        qid = str(question["id"])
        prompt, selected = base.gen.build_prompt(question, base.tables[split], 15)
        if qid not in prompts[split]:
            row = {"id": qid, "prompt": prompt, "selected_tables": selected}
            base.append(target(split) / "prompts.jsonl", row)
            prompts[split][qid] = row
        snapshot("running", f"Calling {split}/{qid}", {"split": split, "id": qid, "question": question["question"]})
        result = request(prompt)
        requested += 1
        result.update({"id": qid, "db": question["db"], "model": MODEL,
                       "provider": "thingo", "timestamp_beijing": base.now()})
        base.append(target(split) / "predictions.jsonl", result)
        predictions[split][qid] = result
        base.append(LEDGER, {"at": base.now(), "split": split, "id": qid,
                             "model": MODEL, "input_tokens": result["input_tokens"],
                             "output_tokens": result["output_tokens"],
                             "api_error": bool(result.get("api_error"))})
        if result.get("api_error"):
            failures += 1
            print(f'[{base.now()}] {split}/{qid}: API error HTTP {result.get("http_status")}', flush=True)
            if base.should_disable_key(result.get("http_status"), result["api_error"]):
                snapshot("paused_quota", "API reports budget or authentication failure")
                return
            if failures >= 4:
                snapshot("paused_api_errors", "Four consecutive API failures")
                return
        else:
            failures = 0
            score(split, question, result)
            print(f'[{base.now()}] {split}/{qid}: saved and scored', flush=True)
        snapshot("running", f"Last completed {split}/{qid}")
        if max_new and requested >= max_new:
            snapshot("paused_probe", f"Completed {requested} probe request")
            return
    snapshot("completed", "All remaining Qwen 3.5 tasks processed")


if __name__ == "__main__":
    lock = socket.socket()
    try:
        lock.bind(("127.0.0.1", 37832))
        main()
    except Exception as exc:
        snapshot("failed", base.safe_error(exc).replace(KEY, "[redacted]"))
        raise
    finally:
        lock.close()
