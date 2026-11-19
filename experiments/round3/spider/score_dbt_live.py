"""Score completed Spider DBT artifacts with the official DuckDB comparator.

If an agent reaches its step cap without Terminate, use its sole changed DuckDB
as the predetermined final artifact. This rule uses no reference answers.
"""

from __future__ import annotations

import html
import json
import sys
import time
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path


warnings.filterwarnings("ignore", category=FutureWarning)
ROOT = Path(r"D:\Spider2\official_spider2")
SUITE = ROOT / "spider2-dbt/evaluation_suite"
sys.path.insert(0, str(SUITE))
from eval_utils import duckdb_match  # noqa: E402

TASKS = [json.loads(line) for line in (ROOT / "spider2-dbt/examples/spider2-dbt.jsonl").open(encoding="utf-8")]
GOLDS = {row["instance_id"]: row for line in (SUITE / "gold/spider2_eval.jsonl").open(encoding="utf-8")
         if (row := json.loads(line))}
RUNS = {
    "Qwen 3.8 Flash": ROOT / "methods/spider-agent-dbt/output/qwen3.8-flash-single_20261118",
    "DeepSeek Flash": ROOT / "methods/spider-agent-dbt/output/deepseek-flash-single_20261118",
}
OUT = Path(r"D:\Spider2\runs")
BEIJING = timezone(timedelta(hours=8))
CACHE = OUT / "dbt_single_score_cache.json"


def artifact(folder: Path, result: dict) -> tuple[Path | None, str]:
    final = str(result.get("result") or "").replace("/workspace/", "")
    if final.endswith(".duckdb"):
        target = (folder / final).resolve()
        if target.is_file() and target.is_relative_to(folder.resolve()):
            return target, "agent_final"
    changed = result.get("result_files", {}).get("changed_files", [])
    choices = [folder / Path(item.replace("\\", "/")).name for item in changed
               if item.lower().endswith(".duckdb")]
    choices = [item for item in choices if item.is_file()]
    if len(choices) == 1:
        return choices[0], "auto_changed_db"
    return None, "no_answer"


def build() -> None:
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    changed_cache = False
    all_rows = {}
    for model, run in RUNS.items():
        rows = []
        for task in TASKS:
            ident = task["instance_id"]
            result_path = run / ident / "spider/result.json"
            gold_meta = GOLDS[ident]["evaluation"]["parameters"]
            gold = SUITE / "gold" / ident / gold_meta["gold"]
            gold_source = "metadata_path"
            if not gold.is_file():
                alternatives = list((SUITE / "gold" / ident).glob("*.duckdb"))
                if len(alternatives) == 1:
                    gold = alternatives[0]
                    gold_source = "unique_gold_file_filename_repair"
            row = {"instance_id": ident, "instruction": task["instruction"],
                   "model": model, "gold_available": gold.is_file(),
                   "gold_source": gold_source,
                   "result_path": result_path.as_uri() if result_path.is_file() else ""}
            if not result_path.is_file():
                row["status"] = "pending"
                rows.append(row)
                continue
            result = json.loads(result_path.read_text(encoding="utf-8"))
            candidate, source = artifact(run / ident, result)
            trajectory = result.get("trajectory") or []
            if not gold.is_file():
                reason = "官方参考 DuckDB 缺失，无法评分"
            elif candidate is None and result.get("finished"):
                reason = "代理已结束，但未提交可评分的 DuckDB 文件"
            elif candidate is None and "Repeated action" in str(result.get("result", "")):
                reason = "代理重复动作中止，未留下可评分文件"
            elif candidate is None and result.get("steps", 0) >= 30:
                reason = "达到 30 步上限，未留下可评分文件"
            elif candidate is None and trajectory and str(trajectory[-1].get("action")) == "None":
                reason = "最后一步动作格式解析失败，未留下可评分文件"
            elif candidate is None:
                reason = "代理提前结束，未留下可评分文件；请看原始轨迹"
            elif source == "auto_changed_db":
                reason = "代理未正式提交；按预设规则选用唯一修改过的 DuckDB"
            else:
                reason = "代理提交 DuckDB，按官方比较器评分"
            row.update({"steps": result.get("steps"), "agent_finished": bool(result.get("finished")),
                        "answer_source": source, "artifact": candidate.as_uri() if candidate else "",
                        "reason": reason})
            if not gold.is_file():
                row["status"] = "reference_unavailable"
            elif candidate is None:
                row["status"] = "no_answer"
            else:
                stat = candidate.stat()
                key = f"{model}|{ident}|{stat.st_size}|{stat.st_mtime_ns}"
                if key not in cache:
                    try:
                        score = duckdb_match(str(candidate), str(gold),
                                             condition_tabs=gold_meta.get("condition_tabs"),
                                             condition_cols=gold_meta.get("condition_cols"),
                                             ignore_orders=gold_meta.get("ignore_orders"))
                        cache[key] = "correct" if score == 1 else "incorrect"
                    except Exception as exc:
                        cache[key] = "evaluator_error:" + str(exc)[:180]
                    changed_cache = True
                row["status"] = cache[key]
            rows.append(row)
        all_rows[model] = rows
    if changed_cache:
        CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    summaries = {}
    for model, rows in all_rows.items():
        counts = {status: sum(row["status"] == status for row in rows)
                  for status in sorted({row["status"] for row in rows})}
        summaries[model] = {"total": len(rows), "completed": len(rows) - counts.get("pending", 0),
                            "reference_available": sum(row["gold_available"] for row in rows),
                            "correct": counts.get("correct", 0), "counts": counts}
    stamp = datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M:%S")
    (OUT / "dbt_live_scores.json").write_text(json.dumps({"updated_beijing": stamp, "summaries": summaries,
                                                         "rows": all_rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    cards = []
    for model, summary in summaries.items():
        done = summary["completed"]
        cards.append(f'<section><h2>{html.escape(model)}</h2><p>已运行 {done}/68 题 · 已核对正确 {summary["correct"]} 题 · 参考库可用 64 题</p>'
                     f'<div class="bar"><span style="width:{100*done/68:.1f}%"></span></div>'
                     f'<p>{html.escape(str(summary["counts"]))}</p></section>')
    body = []
    for model, rows in all_rows.items():
        body.append(f'<h2>{html.escape(model)}</h2><table><tr><th>题号</th><th>状态</th><th>步数</th><th>代理结束</th><th>结果来源</th><th>判定说明</th><th>题目</th></tr>')
        for row in rows:
            link = f'<a href="{html.escape(row["result_path"])}">{html.escape(row["instance_id"])}</a>' if row["result_path"] else html.escape(row["instance_id"])
            body.append(f'<tr><td>{link}</td><td>{html.escape(row["status"])}</td><td>{row.get("steps", "")}</td><td>{"是" if row.get("agent_finished") else "否"}</td><td>{html.escape(row.get("answer_source", ""))}</td><td>{html.escape(row.get("reason", ""))}</td><td>{html.escape(row["instruction"])}</td></tr>')
        body.append('</table>')
    page = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta http-equiv="refresh" content="20"><title>Spider DBT 实时核对</title>
<style>body{{font:17px system-ui;background:#eef4fa;color:#17324e;margin:30px auto;max-width:1500px;padding:0 20px}}section{{background:white;padding:22px;border:1px solid #d7e3ef;border-radius:15px;display:inline-block;vertical-align:top;width:42%;margin:1%}}.bar{{background:#e4eefa;height:16px;border-radius:9px;overflow:hidden}}.bar span{{display:block;background:#3378c8;height:100%}}table{{width:100%;border-collapse:collapse;background:white}}td,th{{border-bottom:1px solid #e1e8f1;padding:10px;text-align:left;vertical-align:top}}th{{position:sticky;top:0;background:#dfeaf7}}a{{color:#1d65ac}}</style>
<h1>Spider 2.0 DBT · 模型实际产物与官方比较</h1><p>北京时间 {stamp} · 每 20 秒更新。代理达到 30 步上限但留下唯一修改过的 DuckDB 时，按预先固定的规则把该文件作为最终产物；表中仍标记“代理结束：否”。官方包缺 4 道题的参考库，显示 reference_unavailable，不计作模型错误；另外 3 道参考库只有文件名与官方清单不一致，按所在题目目录中唯一的 DuckDB 文件核对。</p>{''.join(cards)}{''.join(body)}</html>'''
    (OUT / "dbt_live_scores.html").write_text(page, encoding="utf-8")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    while True:
        build()
        time.sleep(20)
