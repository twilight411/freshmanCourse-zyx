# 第三轮基线与机制代码

- [bird/deepseek_eval.py](bird/deepseek_eval.py)：BIRD 生成与执行评测。SQLite、MySQL、PostgreSQL 分别配置，数据路径见脚本顶部。完整 Dev MySQL 使用 UniQL 本地移植。
- [spider/run_local_benchmark.py](spider/run_local_benchmark.py)：Spider 2.0 Lite 本地 SQLite 子集。配套判分入口为 `score_local_safe.py`，DBT 产物判分见 `score_dbt_live.py`。
- [beaver/run_beaver_full.py](beaver/run_beaver_full.py)：BEAVER 断点续跑与 MySQL 本地评分，`run_beaver_qwen35_remaining.py` 生成独立的补跑结果。
- [soan_smoke/smoke_sqlite.py](soan_smoke/smoke_sqlite.py)：可独立运行的 SQLite 机制冒烟，附构造样例与预写裁决。

基线归档保留运行时的项目路径和依赖。完整复跑需要按脚本配置官方题集、数据库和上游评分器，模型凭据须在本机设置。即开即用的离线核对见仓库根目录 `scripts/verify_results.py` 和冒烟脚本。前轮的完整 BIRD 提示词实验还有独立的[数据准备与复现说明](../../docs/reproduce.md)。

上游：[BIRD](https://bird-bench.github.io/) · [Mini-Dev 2026](https://huggingface.co/datasets/birdsql/bird_mini_dev) · [Spider 2.0](https://github.com/xlang-ai/Spider2) · [BEAVER](https://github.com/beaverbench/beaver)
