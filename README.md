# 朱雅轩｜Text-to-SQL 基线代码与实验结果

本仓库供课程论文阶段检查使用，整理 BIRD / Spider 数据集调研、BIRD Mini-Dev 基线代码、冻结的模型输出和实验结果。整理日期：北京时间 2026-11-17。

## 当前完成情况

| 工作 | 状态 | 材料 |
|---|---|---|
| BIRD / Spider 2.0 调研 | 已完成初步分析 | [调研原稿](reports/benchmark_feasibility.md) |
| BIRD Mini-Dev SQLite 基线 | 已完成 500 题、11 个数据库，两组共 1000 条模型输出 | [基线代码](experiments/pilot/full_official_prompt.py) |
| 结果整理与问题归类 | 已整理官方汇总、逐题结果及至少五类问题 | [实验整理报告](reports/experiment_report.md) |
| Spider 2.0 模型实验 | 尚未提供实测结果 | 不将调研结论写成模型成绩 |
| rsi-flow 结构验证 | 课程私服配置验证、原版依赖恢复；20 项 Java 测试、8 项图检查测试及八模块打包通过 | [结构验证报告](reports/rsi_flow_structure_validation.md)，尚未接入真实模型或 Text-to-SQL 实验 |

## 已有实验结果

模型为 `deepseek-flash`，温度 0，关闭思考，输出上限 4096 token。基线发送官方固定 SQLite 提示词；对照组仅在同一提示词中加入 schema 检查清单。

| 评测口径 | 基线 | 加检查清单 | 差值 |
|---|---:|---:|---:|
| 官方评测首次完整运行，预测和金标合计 30 秒 | 209/500，41.8% | 221/500，44.2% | +2.4 个百分点 |
| 本地逐题评测，每条查询 30 秒 | 210/500，42.0% | 223/500，44.6% | +2.6 个百分点 |

本地配对统计为 62 题改善、49 题回归，精确双侧 McNemar 检验 p=0.2546，尚不能证明稳定提升。两种评测超时规则不同，统计结果不能混用。该实验是提示词对比，尚未实现真实人工反馈或 agentic reasoning。

## 老师查看入口

- [实验整理报告：方法、结果、问题分析、局限和后续计划](reports/experiment_report.md)
- [复现说明：无需 API 的检查、数据准备、重新推理和官方评测](docs/reproduce.md)
- [rsi-flow 代码补丁与一键复现](experiments/rsi-flow/README.md)
- [1000 条冻结模型输出](experiments/pilot/runs/full_official_prompt_deepseek-flash_max4096.jsonl)
- [500 题逐题评分](experiments/pilot/runs/full_official_eval/grades.jsonl)
- 官方汇总：[基线](experiments/pilot/runs/full_official_eval/baseline_official.txt)、[检查清单组](experiments/pilot/runs/full_official_eval/structured_official.txt)
- [原始实验记录（2026-11-05）](reports/full_experiment_result_20261105.md)

## 快速核对

Python 3.10+，从仓库根目录运行。以下命令不调用模型，不需要数据库：

```bash
python scripts/verify_results.py
python experiments/pilot/pilot.py self-check
```

仓库仅提交代码、报告和小型实验记录；数据库和官方数据由复现者从原始来源获取，密钥通过环境变量配置。历史调研文档保留当时状态，以本首页和实验整理报告为当前状态说明。

上游数据和评测代码的使用遵循各自许可，来源与固定版本见 [数据说明](data/PROVENANCE.md) 和 [来源版本记录](reports/source_versions.json)。
