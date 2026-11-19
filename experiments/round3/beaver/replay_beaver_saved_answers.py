"""After quota is exhausted, replay every saved answer against the local database.

This script never calls a model API. It writes an independent, resumable scoring
ledger and leaves the original predictions and scores untouched.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import html
import importlib.util
import json
import os
import time
from collections import Counter
from pathlib import Path

from beaver_score_policy import execution_issue

ROOT = Path(__file__).resolve().parent
SOURCE = Path(r"D:\BeaverBench")
RUN = ROOT / "beaver_full_run"
OUT = RUN / "replay_after_quota"
SPLITS = ("dw_real", "neutron", "nova", "dw")
MODELS = ("deepseek", "qwen")
TZ = datetime.timezone(datetime.timedelta(hours=8))


def now():
    return datetime.datetime.now(TZ).isoformat(timespec="seconds")


def read_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] if path.exists() else []


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def append_json(path: Path, value):
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def replace_html(temp: Path, target: Path):
    for attempt in range(5):
        try:
            temp.replace(target)
            return
        except PermissionError:
            if attempt == 4:
                # A viewer can lock an HTML file on Windows. The scoring ledger
                # and JSON status remain authoritative and must keep progressing.
                print(f"[{now()}] HTML is locked: {target.name}; continuing replay.", flush=True)
            else:
                time.sleep(0.2 * (attempt + 1))


def write_progress(current, completed, total, counts, state="running"):
    esc = lambda value: html.escape(str(value), quote=True)
    percent = 100 * completed / total if total else 100
    page = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta http-equiv="refresh" content="5"><meta name="viewport" content="width=device-width,initial-scale=1"><title>BEAVER 离线复评进度</title><style>
    body{{font:16px/1.6 system-ui,sans-serif;background:#f4f7fb;color:#17324c;margin:0}}main{{max-width:800px;margin:5vh auto;padding:25px;background:white;border-radius:16px}}h1{{margin:0 0 12px}}.bar{{height:14px;background:#e6edf4;border-radius:20px;overflow:hidden}}.bar span{{display:block;height:100%;width:{percent:.2f}%;background:#35a3b7}}.current{{padding:16px;background:#eef6fb;border-radius:10px}}a{{color:#1768b4}}
    </style><main><a href="../progress.html">返回模型进度</a><h1>已保存答案离线复评</h1><p>状态：{esc(state)} · 北京时间 {esc(now())} · 每 5 秒刷新</p><div class="bar"><span></span></div><p><strong>{completed:,} / {total:,}</strong>（{percent:.1f}%）</p><p class="current">当前题目：{esc(current or '已结束')}</p><p>正确 {counts.get('correct', 0):,} · 失分 {counts.get('wrong', 0):,} · 参考异常 {counts.get('gold_error', 0):,} · 执行待复核 {counts.get('execution_unresolved', 0):,}</p><p>只重跑本地参考与模型 SQL，不调用模型 API。</p></main></html>'''
    target = OUT / "progress.html"
    temp = target.with_suffix(".html.tmp")
    temp.write_text(page, encoding="utf-8")
    replace_html(temp, target)


def scorer_module():
    path = SOURCE / "score_whu_sample.py"
    spec = importlib.util.spec_from_file_location("beaver_replay_scorer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def snapshot():
    questions = {split: json.loads((SOURCE / "data" / split / "dev.json").read_text(encoding="utf-8")) for split in SPLITS}
    predictions = {}
    prompts = {}
    old_scores = {}
    fingerprints = {}
    inventory = Counter()
    for split in SPLITS:
        for model in MODELS:
            folder = RUN / f"{split}_{model}"
            file = folder / "predictions.jsonl"
            fingerprints[f"{split}/{model}"] = hashlib.sha256(file.read_bytes()).hexdigest()
            predictions[split, model] = {str(row["id"]): row for row in read_jsonl(file)}
            prompts[split, model] = {str(row["id"]): row for row in read_jsonl(folder / "prompts.jsonl")}
            old_scores[split, model] = {str(row["id"]): row for row in read_jsonl(folder / "scores.jsonl")}
            for q in questions[split]:
                qid = str(q["id"])
                pred = predictions[split, model].get(qid)
                if pred is None:
                    inventory["no_prediction"] += 1
                elif pred.get("api_error"):
                    inventory["api_failure_no_answer"] += 1
                else:
                    inventory["saved_answer"] += 1
                    if not pred.get("predicted_sql"):
                        inventory["answer_without_sql"] += 1
                    if not pred.get("raw_response"):
                        inventory["answer_without_raw_response"] += 1
                    if qid not in prompts[split, model]:
                        inventory["answer_without_prompt"] += 1
                if pred and not pred.get("api_error") and qid not in old_scores[split, model]:
                    inventory["answer_without_prior_score"] += 1
    inventory["total_model_questions"] = sum(len(questions[s]) for s in SPLITS) * len(MODELS)
    return questions, predictions, prompts, old_scores, fingerprints, dict(inventory)


def render_report(summary, done, questions, predictions):
    esc = lambda value: html.escape(str(value), quote=True)
    question_index = {split: {str(q["id"]): q for q in questions[split]} for split in SPLITS}
    changed = [r for r in done.values() if r["original_status"] != r["status"]
               or r["status"] in ("gold_error", "execution_unresolved")]
    changed.sort(key=lambda r: (r["split"], r["id"], r["model"]))
    items = []
    for r in changed:
        q = question_index[r["split"]][str(r["id"])]
        pred = predictions[r["split"], r["model"]][str(r["id"])]
        blocks = (("题目", q.get("question", "")), ("参考 SQL", q["sql"]),
                  ("模型 SQL", pred.get("predicted_sql", "")),
                  ("参考执行错误", r["gold_result"].get("error") or "无"),
                  ("模型执行错误", r["ai_result"].get("error") or "无"))
        body = "".join(f"<h3>{esc(title)}</h3><pre>{esc(value)}</pre>" for title, value in blocks)
        items.append(f'<details><summary>{esc(r["split"])} / {esc(r["model"])} / {esc(r["id"])}：'
                     f'{esc(r["original_status"])} → {esc(r["status"])}</summary>{body}</details>')
    counts = summary["counts"]
    facts = "".join(f"<div><b>{esc(k)}</b><strong>{v:,}</strong></div>" for k, v in counts.items())
    page = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>BEAVER 离线全量复评</title><style>
    body{{margin:0;background:#f4f7fb;color:#17324c;font:15px/1.6 system-ui,sans-serif}}main{{max-width:1080px;margin:auto;padding:28px 18px 50px}}h1{{margin:0}}.muted{{color:#60758a}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:20px 0}}.grid div,details{{background:white;border:1px solid #dce6ef;border-radius:12px;padding:15px}}.grid b,.grid strong{{display:block}}.grid strong{{font-size:25px}}details{{margin:10px 0}}summary{{cursor:pointer;font-weight:650}}h3{{font-size:14px;margin:14px 0 5px}}pre{{background:#eef3f7;padding:12px;border-radius:8px;white-space:pre-wrap;overflow-wrap:anywhere;max-height:330px;overflow:auto}}input{{width:100%;padding:11px;border:1px solid #cbd9e6;border-radius:8px;font:inherit}}a{{color:#1768b4}}
    </style><main><a href="../progress.html">← 返回实时进度</a><h1>已保存答案离线全量复评</h1><p class="muted">北京时间 {esc(summary["updated_beijing"])} · 未调用模型 API · 已复评 {summary["completed"]:,} 条保存的答案；API 失败或未运行的题目没有模型答案。</p><div class="grid">{facts}</div><p>可核验样本的局部 EX：{summary["clean_partial_ex"]:.2%}。分母只包括 correct + wrong；gold_error 和 execution_unresolved 单列。实验未覆盖完整 15,956 条模型题，不能称为全量最终成绩。</p><h2>与原评分不同或仍待复核的题目：{len(changed):,}</h2><input id="search" placeholder="搜索题号、模型或状态"><div id="items">{''.join(items)}</div><script>document.getElementById('search').oninput=e=>{{let q=e.target.value.toLowerCase();document.querySelectorAll('details').forEach(x=>x.hidden=!x.querySelector('summary').textContent.toLowerCase().includes(q))}}</script></main></html>'''
    target = OUT / "report.html"
    temp = target.with_suffix(".html.tmp")
    temp.write_text(page, encoding="utf-8")
    replace_html(temp, target)


def run(dry_run=False):
    questions, predictions, prompts, old_scores, fingerprints, inventory = snapshot()
    if dry_run:
        print(json.dumps({"inventory": inventory, "ready_for_replay": inventory["saved_answer"]}, ensure_ascii=False))
        return
    OUT.mkdir(parents=True, exist_ok=True)
    manifest_path = OUT / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest["prediction_sha256"] != fingerprints:
            raise RuntimeError("Prediction files changed after replay started; use a new output directory.")
    else:
        manifest = {"started_beijing": now(), "prediction_sha256": fingerprints,
                    "inventory": inventory, "scoring_policy": "saved-answer-replay-v1",
                    "model_api_calls": 0, "source_database": r"D:\BeaverBenchRuntime_20261116"}
        atomic_json(manifest_path, manifest)
    scored_path = OUT / "scores.jsonl"
    done = {(r["split"], r["model"], str(r["id"])): r for r in read_jsonl(scored_path)}
    scorer = scorer_module()
    password = scorer.password()
    import mysql.connector
    connection = mysql.connector.connect(host="127.0.0.1", port=33306, user="root", password=password, connection_timeout=5)
    connection.close()
    counts = Counter(r["status"] for r in done.values())
    total_answers = inventory["saved_answer"]
    write_progress(None, len(done), total_answers, counts)
    for split in SPLITS:
        for q in questions[split]:
            qid = str(q["id"])
            gold_cache = None
            for model in MODELS:
                pred = predictions[split, model].get(qid)
                if not pred or pred.get("api_error") or (split, model, qid) in done:
                    continue
                atomic_json(OUT / "status.json", {"state": "running", "current": f"{split}/{model}/{qid}",
                                                  "completed": len(done), "total_answers": total_answers,
                                                  "counts": dict(counts), "updated_beijing": now()})
                write_progress(f"{split}/{model}/{qid}", len(done), total_answers, counts)
                db = "dw" if split == "dw_real" else split
                if gold_cache is None:
                    gold_cache = scorer.execute(q["sql"], db, password)
                gold, gold_df = gold_cache
                if execution_issue(gold.get("error")) == "database_connection":
                    atomic_json(OUT / "status.json", {"state": "blocked_database", "current": f"{split}/{model}/{qid}",
                                                      "completed": len(done), "total_answers": total_answers,
                                                      "error": gold["error"], "updated_beijing": now()})
                    write_progress(f"{split}/{model}/{qid}", len(done), total_answers, counts, "blocked_database")
                    return
                result, pred_df = scorer.execute(pred.get("predicted_sql", ""), db, password)
                if execution_issue(result.get("error")) == "database_connection":
                    atomic_json(OUT / "status.json", {"state": "blocked_database", "current": f"{split}/{model}/{qid}",
                                                      "completed": len(done), "total_answers": total_answers,
                                                      "error": result["error"], "updated_beijing": now()})
                    write_progress(f"{split}/{model}/{qid}", len(done), total_answers, counts, "blocked_database")
                    return
                if gold.get("error"):
                    state, comparison = "gold_error", "Reference SQL execution failed"
                elif execution_issue(result.get("error")):
                    state, comparison = "execution_unresolved", result["error"]
                elif result.get("error"):
                    state, comparison = "wrong", result["error"]
                else:
                    match, comparison = scorer.compare_results(pred_df, gold_df)
                    state = "correct" if match else "wrong"
                row = {"split": split, "model": model, "id": qid, "status": state, "comparison": comparison,
                       "predicted_sql_sha256": hashlib.sha256(pred.get("predicted_sql", "").encode()).hexdigest(),
                       "gold_sql_sha256": hashlib.sha256(q["sql"].encode()).hexdigest(),
                       "gold_result": gold, "ai_result": result,
                       "original_status": old_scores[split, model].get(qid, {}).get("status"),
                       "scored_beijing": now()}
                append_json(scored_path, row)
                done[split, model, qid] = row
                counts[state] += 1
                print(f"[{now()}] {len(done)}/{total_answers} {split}/{model}/{qid}: {state}", flush=True)
    atomic_json(OUT / "status.json", {"state": "completed", "completed": len(done), "total_answers": total_answers,
                                      "counts": dict(counts), "updated_beijing": now()})
    write_progress(None, len(done), total_answers, counts, "completed")
    both_empty_with_error=sum(1 for r in done.values()
                              if (r["gold_result"].get("error") or r["gold_result"].get("row_count")==0)
                              and (r["ai_result"].get("error") or r["ai_result"].get("row_count")==0)
                              and (r["gold_result"].get("error") or r["ai_result"].get("error")))
    summary={"inventory": inventory, "completed": len(done), "counts": dict(counts),
                                       "clean_partial_ex": (counts["correct"] / (counts["correct"] + counts["wrong"])
                                                            if counts["correct"] + counts["wrong"] else None),
                                       "official_error_as_empty_potential_false_positive": both_empty_with_error,
                                       "original_status_changes": dict(Counter(f'{r["original_status"]}->{r["status"]}' for r in done.values()
                                                                          if r["original_status"] != r["status"])),
                                       "updated_beijing": now(), "model_api_calls": 0}
    atomic_json(OUT / "summary.json", summary)
    render_report(summary, done, questions, predictions)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args()
    run(arguments.dry_run)
