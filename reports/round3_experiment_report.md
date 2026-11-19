# 第三轮实验结果与分析

2026-11-19（北京时间）。本轮冻结本地基线评分和独立机制实验结果。[结果三线表](results_20261119.html) · [分子、分母及覆盖 CSV](results_20261119.csv)

## 基线结果与判分

- **BIRD**　完整 Dev 2024、修订 Dev 2025 和 Mini-Dev 2026 分开统计。Mini-Dev 的 SQLite、MySQL、PostgreSQL 使用同批题目的不同方言。DeepSeek Flash High 在这三列的 EX 为 65.40%、66.60%、68.20%，Qwen 3.8 Flash Off 为 31.40%、30.20%、32.40%。完整 Dev MySQL 是本地移植。EX 使用完整题集分母，表内标出参考异常。模型与思考设置不同，差值无法单独归因于模型或 SQL 方言。
- **Spider 2.0**　Lite 仅覆盖本地 SQLite 135 题。DeepSeek 官方 High、DeepSeek 武大 Off、Qwen Flash 武大 Off 分别为 69、59、37 题正确。DBT 分开报告 SR，DeepSeek 与 Qwen 3.8 分别为 4/64、6/64。每轮 68 项任务，4 项缺参考数据库。可评任务中分别有 46、44 项无有效产物。
- **BEAVER**　这是阶段快照。三路模型独立列示，Qwen 3.5 采用原 Qwen 续跑启动时未覆盖的子集。报告的本地 EX 以正确加失分为分母，排除参考异常和执行待复核。已评分数量含生成达到输出上限但未提取到 SQL 的记录，实际可用 SQL 数需另核。不同模型覆盖范围有差异，暂不做整套横向排名。

## 错误与改进

1. **表和字段定位**　检查提示中是否包含所需表、筛选列和连接键。把检索缺失、选错字段、连接路径错误拆开。
2. **条件与聚合**　核对真实值写法、NULL、分母和聚合粒度，比较模型查询与参考查询执行结果。结构差异只作为排查线索。
3. **完成情况与评分异常**　分别记录生成截断、SQL 执行错误、参考异常、环境异常和缺失产物。对参考或环境异常优先复跑评分，再作归因。
4. **证据约束验证**　SOAN 启发的原型在 SQLite 完成 14 项机制冒烟。冻结留出集的四组条件在同一研究窗口各用 24 道题；单次澄清开启、提前范围证据关闭时，策略组 20/24、对照 12/24，有害复用 0/12 次。双开时两组均为 24/24。该试验用了预置裁决与固定范围证据，结论限于当前确定性机制。[留出集四组结果](soan_holdout_20261119.md)

前一轮 BIRD Mini-Dev 提示词实验的本地配对结果为 62 改善、49 回归，精确双侧 McNemar 检验 p=0.2546。该实验与本轮多模型基线、SOAN 留出实验各自使用独立设置。[原实验及逐题证据](experiment_report.md)

## 表格与来源

分组表头和 EX 列参考 [BIRD 论文表 2](https://proceedings.neurips.cc/paper_files/paper/2023/file/83fc8fab1710363050bbd1d4b8cc0021-Paper-Datasets_and_Benchmarks.pdf) 与 [Spider 2.0 作者论文表 4–6](https://proceedings.iclr.cc/paper_files/paper/2025/file/46c10f6c8ea5aa6f267bcdabcb123f97-Paper-Conference.pdf)。模型与指标的紧凑排版参考 [MSc-SQL，NAACL 2025，表 1](https://aclanthology.org/2025.naacl-long.107.pdf)。BEAVER 版本及本地口径见 [官方基准](https://beaverbench.github.io/)。
