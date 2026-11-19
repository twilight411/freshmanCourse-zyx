# 第三轮 · Text-to-SQL 数据集工作与实验分析

2026-11-19（北京时间）。汇总截至该日的本地基线与评分记录。[结果三线表](results_20261119.html) · [分子、分母及覆盖 CSV](results_20261119.csv)

## 数据集准备与覆盖

| 基准 | 本地工作范围 | 待补覆盖 |
| --- | --- | --- |
| BIRD | 完整 Dev 2024 的 SQLite 和本地 MySQL、修订 Dev 2025 的 SQLite、Mini-Dev 2026 的 SQLite、MySQL 和 PostgreSQL | 本报告聚焦 Dev；Train 与 Test 未纳入 |
| Spider 2.0 | Lite 本地 SQLite 135 题、DBT 本地 DuckDB 68 项；逐轮保留模型 SQL 或工作流产物 | Lite BigQuery、Lite Snowflake 和独立 Snow 数据组未纳入；DBT 每轮有 4 项缺参考数据库 |
| BEAVER | DW Real、Neutron、Nova 和 DW；保留各模型的独立进度，支持断点续跑和已有答案复评分 | DW 和部分模型组合仍有未覆盖记录，详见阶段表 |

## 基线结果与判分

- **BIRD**　完整 Dev 2024、修订 Dev 2025 和 Mini-Dev 2026 分开统计。Mini-Dev 的 SQLite、MySQL、PostgreSQL 使用同批题目的不同方言。DeepSeek Flash High 在这三列的 EX 为 65.40%、66.60%、68.20%，Qwen 3.8 Flash Off 为 31.40%、30.20%、32.40%。完整 Dev MySQL 是本地移植。EX 使用完整题集分母，表内标出参考异常。模型与思考设置不同，差值无法单独归因于模型或 SQL 方言。
- **Spider 2.0**　Lite 仅覆盖本地 SQLite 135 题。DeepSeek 官方 High、DeepSeek 武大 Off、Qwen Flash 武大 Off 分别为 69、59、37 题正确。DBT 分开报告 SR，DeepSeek 与 Qwen 3.8 分别为 4/64、6/64。每轮 68 项任务，4 项缺参考数据库。可评任务中分别有 46、44 项无有效产物。
- **BEAVER**　这是阶段快照。三路模型独立列示，Qwen 3.5 采用原 Qwen 续跑启动时未覆盖的子集。报告的本地 EX 以正确加失分为分母，排除参考异常和执行待复核。已评分数量含生成达到输出上限但未提取到 SQL 的记录，实际可用 SQL 数需另核。不同模型覆盖范围有差异，暂不做整套横向排名。

## 逐题整理与评分异常排查

1. **表和字段定位**　检查提示中是否包含所需表、筛选列和连接键。把检索缺失、选错字段、连接路径错误拆开。
2. **条件与聚合**　核对真实值写法、NULL、分母和聚合粒度，比较模型查询与参考查询执行结果。结构差异只作为排查线索。
3. **完成情况与评分异常**　分别记录生成截断、SQL 执行错误、参考异常、环境异常和缺失产物。对参考或环境异常优先复跑评分，再作归因。
逐题审查使用模型 SQL、参考 SQL 和可用的执行证据。汇总表保留各轮分母与评分覆盖，便于复查结构差异、失分和评分异常。

## 表格与来源

分组表头和 EX 列参考 [BIRD 论文表 2](https://proceedings.neurips.cc/paper_files/paper/2023/file/83fc8fab1710363050bbd1d4b8cc0021-Paper-Datasets_and_Benchmarks.pdf) 与 [Spider 2.0 作者论文表 4–6](https://proceedings.iclr.cc/paper_files/paper/2025/file/46c10f6c8ea5aa6f267bcdabcb123f97-Paper-Conference.pdf)。模型与指标的紧凑排版参考 [MSc-SQL，NAACL 2025，表 1](https://aclanthology.org/2025.naacl-long.107.pdf)。BEAVER 版本及本地口径见 [官方基准](https://beaverbench.github.io/)。
