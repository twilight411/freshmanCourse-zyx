# BIRD Mini-Dev 2026 第一组全量实验结果

**核对时间：北京时间 2026-11-05。** 数据版本、哈希和服务器路径见 `research/data/PROVENANCE.md`。全部 500 条 SQLite 问题、11 个数据库。DeepSeek `deepseek-flash`，温度 0，关闭思考模式，两组统一 4096 个输出 token。

## 方法

基线直接发送 BIRD 发布的 2026 固定提示词。实验组在相同提示词的 `Output Format` 段落前插入一段结构化 schema 检查清单；问题、证据、数据库、模型和解码参数一致。预测逐条冻结后才读取金标 SQL 评分。这是结构化提示实验，没有采集真实人工反馈，也没有组里尚未收到的第一版代码。

## 结果

| 评分 | 基线 | 结构化 | 差值 |
|---|---:|---:|---:|
| BIRD 官方评测，30 秒组合查询超时 | 209/500 = 41.8% | 221/500 = 44.2% | +12 题，+2.4 个百分点 |
| 本地独立逐题评测，单条查询 30 秒超时 | 210/500 = 42.0% | 223/500 = 44.6% | +13 题，+2.6 个百分点 |

本地评测的配对明细：62 题由错变对，49 题由对变错；精确双侧 McNemar 检验 **p = 0.2546**。排除先前用于检查流程的 20 题后，剩余 480 题基线 198/480、结构化 209/480，差值 +2.29 个百分点，配对 p = 0.3382。这些结果是正向线索，尚不足以证明稳定提高 2–5 个百分点。

官方评分器第二次逐题复跑时，基线仍为 209/500，结构化组变为 222/500；至少有 1 题受运行时差异影响。官方评分器对预测和金标 SQL **合计**设置 30 秒超时，本地评分分别执行，故总分相差 1–2 题。主表采用首次完整官方评测分数；本地配对统计用于判断趋势，不能冒充官方逐题配对统计。

1000 次模型调用全部完成，1 条回答未能提取 SQL；4 条达到 4096-token 上限。500 条金标 SQL 在修正本地计算步数上限后均成功执行。此前 768-token 调试轮次出现截断，已停止且不计入结果。

## 判断与下一步

这个结果说明单纯加检查清单有一点潜力，但 49 题回归也不少。下一组应针对失败题做机制分析，并加入可审计的数据库级 schema 知识或执行反馈。若做“人工反馈”研究，需要人实际撰写、冻结并标记反馈，不能把模型生成的提示词称作人工反馈。BEAVER 的金标 oracle 提示也不能当成真实用户反馈。

目前的结果只涉及 schema 引导提示，尚未实现题目中的 agentic reasoning。四个候选题目继续保留，等数据库级知识或执行反馈实验形成稳定结果后再定。

## 可复核文件

- `research/experiments/pilot/runs/full_official_prompt_deepseek-flash_max4096.jsonl`：1000 条冻结的模型回答、SQL 和请求元数据。
- `research/experiments/pilot/runs/full_official_eval/baseline_official.txt` 与 `structured_official.txt`：两组 BIRD 官方汇总。
- `research/experiments/pilot/runs/full_official_eval/grades.jsonl` 与 `summary.md`：本地逐题评分和配对统计。
- `research/experiments/pilot/full_official_prompt.py`、`grade_full.py`、`official_per_item.py`：生成、独立评分和官方逐题复核代码。

官方基准：[BIRD Mini-Dev 数据集](https://huggingface.co/datasets/birdsql/bird_mini_dev)、[评测代码](https://github.com/bird-bench/mini_dev)。
