"""Deterministic, offline SOAN × Text-to-SQL smoke test. Only stdlib is used."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
FIXTURE = ROOT / "smoke_fixture.json"
ADJUDICATIONS = ROOT / "smoke_adjudications.json"
RESULT = ROOT / "smoke_result.json"
SUMMARY = ROOT / "smoke_summary.md"
BEIJING = timezone(timedelta(hours=8))

SQL = {
    "unrepaired": "SELECT subtotal_cents FROM orders WHERE case_id = ?",
    "charge_on_order": "SELECT subtotal_cents + shipping_cents FROM orders WHERE case_id = ?",
    "charge_on_ship": "SELECT subtotal_cents + CASE WHEN status = 'shipped' THEN shipping_cents ELSE 0 END FROM orders WHERE case_id = ?",
}


def predict(db: sqlite3.Connection, candidate: str, case_id: str) -> int:
    row = db.execute(SQL[candidate], (case_id,)).fetchone()
    if row is None:
        raise ValueError(f"missing case: {case_id}")
    return int(row[0])


def assert_check(checks: list[dict], name: str, condition: bool, detail: object) -> None:
    checks.append({"name": name, "passed": bool(condition), "detail": detail})
    if not condition:
        raise AssertionError(f"{name}: {detail}")


def apply_evidence(db: sqlite3.Connection, state: dict, task_id: str,
                   evidence: dict | None, candidates: list[str]) -> dict:
    """Apply only evidence exposed in this phase; case evidence never activates a rule."""
    outcome = {"case_repair": False, "answer_deferred": True, "update_deferred": True,
               "survivors_for_this_case": [], "repair_sql": None, "repair_value_cents": None}
    if evidence is None:
        return outcome
    if evidence["kind"] == "case":
        if evidence["scope"] != {"task_id": task_id}:
            raise ValueError("case evidence must be scoped to this task")
        matches = [h for h in candidates if predict(db, h, task_id) == evidence["value_cents"]]
        outcome["survivors_for_this_case"] = matches
        if matches:
            chosen = matches[0]
            # The local repair is a real SQL execution, not a copied oracle value.
            actual = predict(db, chosen, task_id)
            state["case_repairs"][task_id] = {"candidate": chosen, "value_cents": actual,
                                               "evidence_scope": evidence["scope"]}
            outcome.update(case_repair=True, answer_deferred=False,
                           repair_sql=SQL[chosen], repair_value_cents=actual)
        return outcome
    if evidence["kind"] == "rule":
        scope = evidence["scope"]
        candidate = evidence["candidate"]
        if candidate not in candidates or not scope or task_id not in state["case_repairs"]:
            raise ValueError("rule evidence lacks candidate, scope or prior case repair")
        if predict(db, candidate, task_id) != state["case_repairs"][task_id]["value_cents"]:
            raise ValueError("rule contradicts adjudicated case")
        state["active_rules"].append({"candidate": candidate, "scope": scope,
                                      "evidence": "F2_rule", "effective_time": evidence["effective_time"]})
        outcome.update(case_repair=True, answer_deferred=False, update_deferred=False,
                       repair_sql=SQL[candidate], repair_value_cents=predict(db, candidate, task_id))
        return outcome
    raise ValueError(f"unknown evidence kind: {evidence['kind']}")


def route(db: sqlite3.Connection, state: dict, case: dict) -> dict:
    """Read only activated state; this function has no adjudication argument."""
    candidate = "charge_on_ship"
    for rule in state["active_rules"]:
        if all(case.get(field) == value for field, value in rule["scope"].items()):
            candidate = rule["candidate"]
            break
    return {"candidate": candidate, "value_cents": predict(db, candidate, case["id"])}


def main() -> None:
    raw = FIXTURE.read_bytes()
    fixture = json.loads(raw)
    cases = fixture["cases"]
    adjudication_raw = ADJUDICATIONS.read_bytes()
    adjudications = json.loads(adjudication_raw)["adjudications"]
    db = sqlite3.connect(":memory:")
    db.execute("""CREATE TABLE orders (
        case_id TEXT PRIMARY KEY, version TEXT NOT NULL, channel TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('pending', 'shipped')),
        subtotal_cents INTEGER NOT NULL CHECK (subtotal_cents >= 0),
        shipping_cents INTEGER NOT NULL CHECK (shipping_cents >= 0),
        customer_id TEXT NOT NULL
    )""")
    db.executemany(
        "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(c["id"], c["version"], c["channel"], c["status"],
          c["subtotal_cents"], c["shipping_cents"], "customer_1") for c in cases],
    )
    db.execute("CREATE TABLE order_items (case_id TEXT NOT NULL, item_id TEXT NOT NULL, PRIMARY KEY(case_id, item_id))")
    db.executemany("INSERT INTO order_items VALUES (?, ?)", [("F1", "item_1"), ("F1", "item_2")])

    checks: list[dict] = []
    events: list[dict] = []
    candidates = ["charge_on_order", "charge_on_ship"]
    state: dict = {"case_repairs": {}, "active_rules": []}
    by_id = {case["id"]: case for case in cases}
    probe_history: list[dict] = []

    def probe(phase: str, expected_candidate: str, expected_value: int) -> None:
        observed = route(db, state, by_id["TRANSFER"])
        probe_history.append({"phase": phase, "task_id": "TRANSFER", **observed})
        assert_check(checks, f"{phase}：未见迁移探针按当前规则路由",
                     observed == {"candidate": expected_candidate, "value_cents": expected_value}, observed)

    def record(phase: str, task_id: str, outcome: dict, **extra: object) -> None:
        events.append({"phase": phase, "task_id": task_id, **outcome,
                       "knowledge_activation": len(state["active_rules"]), **extra})
    predictions = {c["id"]: {h: predict(db, h, c["id"]) for h in candidates} for c in cases}
    assert_check(checks, "F0 零运费不可区分", predictions["F0"][candidates[0]] == predictions["F0"][candidates[1]] == 10000, predictions["F0"])

    # Only the F1 adjudication is revealed at this stage. A correct case repair
    # does not imply that one of the two semantic rules has been identified.
    f1_value = adjudications["F1"]["value_cents"]
    f1_outcome = apply_evidence(db, state, "F1", adjudications["F1"], candidates)
    f1_survivors = f1_outcome["survivors_for_this_case"]
    record("F1_feedback", "F1", f1_outcome)
    assert_check(checks, "F1 当前题 SQL 修复且两候选保留",
                 predict(db, "unrepaired", "F1") != f1_value and
                 f1_outcome["case_repair"] and f1_outcome["repair_value_cents"] == f1_value and
                 len(f1_survivors) == 2 and not state["active_rules"],
                 {"unrepaired_cents": predict(db, "unrepaired", "F1"), "feedback_cents": f1_value,
                  "outcome": f1_outcome, "activation_count": len(state["active_rules"])})
    probe("F1 后", "charge_on_ship", 20000)

    # The same F2 instance is run first with no adjudication, then case-only,
    # then explicit rule-level adjudication; no future answer is exposed early.
    f2 = predictions["F2"]
    no_outcome = apply_evidence(db, state, "F2", None, candidates)
    record("F2_no_adjudication", "F2", no_outcome, predictions_cents=f2)
    assert_check(checks, "F2 无裁决时区分 100/110 且不更新",
                 set(f2.values()) == {10000, 11000} and no_outcome["answer_deferred"] and
                 no_outcome["update_deferred"] and not state["active_rules"],
                 {"predictions_cents": f2, "outcome": no_outcome})
    probe("F2 无裁决后", "charge_on_ship", 20000)

    case_judgment = adjudications["F2_case"]
    case_outcome = apply_evidence(db, state, "F2", case_judgment, candidates)
    record("F2_case_adjudication", "F2", case_outcome, evidence_scope=case_judgment["scope"])
    assert_check(checks, "单题裁决实际执行 F2 SQL，不扩写规则",
                 case_outcome["case_repair"] and len(case_outcome["survivors_for_this_case"]) == 1 and
                 case_outcome["repair_value_cents"] == 11000 and
                 state["case_repairs"]["F2"]["value_cents"] == 11000 and
                 not state["active_rules"] and case_judgment["scope"] == {"task_id": "F2"},
                 {"outcome": case_outcome, "activation_count": len(state["active_rules"])})
    probe("F2 单题裁决后", "charge_on_ship", 20000)

    rule_judgment = adjudications["F2_rule"]
    assert_check(checks, "规则裁决与 F1/F2 回放相容", rule_judgment["kind"] == "rule" and
                 rule_judgment["candidate"] in f1_survivors and
                 predictions["F2"][rule_judgment["candidate"]] == case_judgment["value_cents"], rule_judgment)
    rule_outcome = apply_evidence(db, state, "F2", rule_judgment, candidates)
    record("F2_rule_adjudication", "F2", rule_outcome, evidence_scope=rule_judgment["scope"])
    assert_check(checks, "范围化规则裁决才实际激活", len(state["active_rules"]) == 1 and
                 not rule_outcome["update_deferred"], state["active_rules"])
    probe("F2 范围化规则裁决后", "charge_on_order", 20300)

    routed = {c["id"]: route(db, state, c)
              for c in cases if c["id"] in {"TRANSFER", "OUT_OF_SCOPE", "HISTORICAL"}}
    assert_check(checks, "未见范围内迁移题采用新规则", routed["TRANSFER"] == {"candidate": "charge_on_order", "value_cents": 20300}, routed["TRANSFER"])
    assert_check(checks, "范围外任务仍走旧规则", routed["OUT_OF_SCOPE"] == {"candidate": "charge_on_ship", "value_cents": 20000}, routed["OUT_OF_SCOPE"])
    assert_check(checks, "历史版本仍走旧规则", routed["HISTORICAL"] == {"candidate": "charge_on_ship", "value_cents": 20000}, routed["HISTORICAL"])

    customer_sum = "SELECT o.customer_id, SUM(o.subtotal_cents + o.shipping_cents) AS payable_cents"
    customer_group = " WHERE o.case_id = ? GROUP BY o.customer_id"
    naive_sql = customer_sum + " FROM orders o JOIN order_items i ON i.case_id = o.case_id" + customer_group
    repaired_sql = (customer_sum + " FROM orders o JOIN "
                    "(SELECT case_id, COUNT(*) AS item_count FROM order_items GROUP BY case_id) i "
                    "ON i.case_id = o.case_id" + customer_group)
    naive = db.execute(naive_sql, ("F1",)).fetchone()
    repaired = db.execute(repaired_sql, ("F1",)).fetchone()
    item_count = db.execute("SELECT COUNT(*) FROM order_items WHERE case_id = ?", ("F1",)).fetchone()[0]
    join_result = {"naive": {"customer_id": naive[0], "payable_cents": naive[1]},
                   "repaired": {"customer_id": repaired[0], "payable_cents": repaired[1], "item_count": item_count},
                   "reused_customer_summary_sql": customer_sum + customer_group,
                   "naive_sql": naive_sql, "repaired_sql": repaired_sql,
                   "dag_change": {"inserted": ["preaggregate_items_by_order"],
                                  "retained": ["customer_level_sum"], "replaced": ["raw_items_join"]}}
    assert_check(checks, "两明细 JOIN 翻倍，预聚合后复用客户汇总 SQL 且金额正确",
                 naive[1] == 22000 and repaired == ("customer_1", 11000) and item_count == 2 and
                 naive_sql.startswith(customer_sum) and repaired_sql.startswith(customer_sum) and
                 naive_sql.endswith(customer_group) and repaired_sql.endswith(customer_group), join_result)
    events.append({"phase": "join_grain_repair", "task_id": "F1", "dag_change": join_result["dag_change"],
                   "before_cents": naive[1], "after_cents": repaired[1]})

    result = {"status": "passed", "run_time_beijing": datetime.now(BEIJING).isoformat(timespec="seconds"),
              "fixture_sha256": hashlib.sha256(raw).hexdigest(),
              "adjudications_sha256": hashlib.sha256(adjudication_raw).hexdigest(),
              "sqlite_version": sqlite3.sqlite_version,
              "fixture_source": fixture["source"], "sql": SQL, "predictions_cents": predictions,
              "routed_cents": routed, "probe_history": probe_history,
              "case_repairs": state["case_repairs"], "activated_rules": state["active_rules"], "events": events,
              "join_result": join_result, "checks": checks, "passed_count": sum(c["passed"] for c in checks)}
    RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# SOAN × Text-to-SQL 离线 SQLite 冒烟", "", f"运行时间（北京时间）：{result['run_time_beijing']}",
             f"结果：{result['passed_count']}/{len(checks)} 项通过。SQLite {sqlite3.sqlite_version}。", "",
             f"- F1：原查询 10000 分；裁决 11000 分；两候选均保留，长期激活 0。",
             "- F2 无裁决：候选分别为 11000／10000 分，暂缓回答和长期更新。",
             "- F2 单题裁决：当前题可修，规则仍未激活；范围化规则裁决后，仅 v2／retail 启用下单计运费。",
             "- 同一未见迁移探针：F1 后、F2 无裁决后、F2 单题裁决后均为旧规则 20000 分；范围化规则裁决后为 20300 分。",
             "- 范围外及历史版本各 20000 分，继续旧规则。",
             "- 两明细 JOIN：直接聚合 22000 分；按订单预聚合后 11000 分；两个查询共用同一客户汇总 SQL 片段。", "",
             "## 可复算文件", "", "- `smoke_fixture.json`：不含裁决的样例。",
             "- `smoke_adjudications.json`：独立预写裁决；按阶段露出。",
             "- `smoke_sqlite.py`：SQL、阶段状态和断言。",
             "- `smoke_result.json`：逐项检查、SQL、预测、路由和事件。", "",
             "## 研究边界", "", "这只是预写裁决的确定性机制冒烟；未测模型、真实基准、配对净收益或未知语义下的归因能力。", ""]
    SUMMARY.write_text("\n".join(lines), encoding="utf-8")
    print(f"PASS {result['passed_count']}/{len(checks)} | {RESULT} | {SUMMARY}")


if __name__ == "__main__":
    main()
