# 朱雅轩｜Text-to-SQL 论文实验

新人培养计划 · 第 3 轮 · 2026-11-19（北京时间）

## 最新结果

![BIRD 完整与修订 Dev、Mini-Dev 三方言，Spider 2.0 Lite 和 DBT，以及 BEAVER 分组阶段结果三线表](reports/results_20261119.png)

[HTML 表格](reports/results_20261119.html) · [CSV](reports/results_20261119.csv) · [LaTeX booktabs](reports/results_20261119_booktabs.tex) · [第三轮实验分析](reports/round3_experiment_report.md)

## 方法验证

- **BIRD Mini-Dev SQLite 提示词对照**　官方 EX 41.8% → 44.2%（500 题）。本地复核的配对差异未达显著。 [代码与分析](reports/experiment_report.md)
- **SOAN 启发的证据门控**　SQLite 机制冒烟 14/14。冻结留出集在「开启单次澄清、关闭提前范围证据」条件下，策略组 20/24，对照组 12/24。 [留出集结果](reports/soan_holdout_20261119.md)

## 复现入口

- [基线脚本与复现条件](experiments/round3/README.md)
- [BIRD Mini-Dev 固定输出与复现](docs/reproduce.md)
- [rsi-flow Java 结构验证](experiments/rsi-flow/README.md)
- [数据来源与版本](data/PROVENANCE.md)

```bash
python scripts/verify_results.py
python experiments/round3/soan_smoke/smoke_sqlite.py
```
