# 复现说明

所有命令从本仓库根目录运行，Python 3.10+。推理与本地评测只使用 Python 标准库；官方评分器另有依赖。

## 1. 核对现有结果（无需联网或密钥）

```bash
python scripts/verify_results.py
python experiments/pilot/pilot.py self-check
```

前者检查文件哈希、1000 条输出的完整配对、500 题评分、本地统计与官方汇总；它不重新执行 SQL。后者在临时 SQLite 数据库上验证执行器和抽取器，不代表基准成绩。

## 2. 准备官方数据

```bash
python scripts/prepare_data.py
```

仅下载固定版本的 SQLite 标注和官方推理输入，校验 SHA-256；不会下载数据库或调用模型。数据源为 Hugging Face 的 `birdsql/bird_mini_dev`，固定 revision `5b256b5a9c17478f06ed6912612e9a14d17065c8`。

数据库另从 [BIRD 官方 minidev.zip](https://bird-bench.oss-cn-beijing.aliyuncs.com/minidev.zip) 获取（约 801 MB 压缩包），MD5 应为 `7beb6dab11e65f0fde563e644a1ea319`。仅将 `dev_databases` 及其描述文件解压到以下布局，数据库不要提交到 Git：

```text
data/bird_mini_dev_db/dev_databases/
  california_schools/california_schools.sqlite
  ...其余 10 个数据库目录...
```

## 3. 根据冻结输出重新执行本地评分

```bash
python experiments/pilot/grade_full.py
python experiments/pilot/summarize_full.py
```

这些命令会覆盖本地 `full_official_eval/grades.jsonl` 和 `summary.md`，并生成官方评分输入。先保存现有记录或在独立克隆中运行。依赖数据库，耗时受设备影响；不调用模型。

## 4. 使用官方评分器

```bash
git clone https://github.com/bird-bench/mini_dev.git sources/mini_dev
git -C sources/mini_dev checkout 1c49a375e76c471e218564da3f4617513d33e371
```

按上游说明安装评分所需依赖。完成第 3 步后，基线命令为：

```bash
python sources/mini_dev/evaluation/evaluation_ex.py --predicted_sql_path experiments/pilot/runs/full_official_eval/baseline.json --ground_truth_path experiments/pilot/runs/full_official_eval/gold.sql --db_root_path data/bird_mini_dev_db/dev_databases/ --num_cpus 2 --meta_time_out 30 --diff_json_path experiments/pilot/runs/full_official_eval/difficulty.jsonl --output_log_path experiments/pilot/runs/full_official_eval/baseline_official_rerun.txt
```

检查清单组把 `baseline.json` 换成 `structured.json`，日志改为 `structured_official_rerun.txt`。如需官方逐题复核，可运行 `python experiments/pilot/official_per_item.py`。超时可能导致重跑成绩略有变化，归档成绩保持首次完整官方评测口径。

## 5. 重新调用模型（会产生 API 费用）

`full_official_prompt.py` 默认续跑已存在的结果。仓库已有完整输出，因此直接执行会跳过全部题目。要重新采样，请先将原有 `full_official_prompt_deepseek-flash_max4096.jsonl` 移至备份目录，再运行。

仅在自己的终端环境设置 `ANTHROPIC_AUTH_TOKEN`，不要写入代码或 Git。以 PowerShell 为例，隐藏输入：

```powershell
$credential = Get-Credential -UserName api -Message '密码栏输入 API Key'
$env:ANTHROPIC_AUTH_TOKEN = $credential.GetNetworkCredential().Password
python experiments/pilot/full_official_prompt.py
Remove-Item Env:ANTHROPIC_AUTH_TOKEN
```

共 500 题 × 2 组，使用脚本中固定模型和参数。`deepseek-flash` 是服务商模型别名，未来是否仍可用、输出是否相同无法保证；更换模型后须单列新实验，不能覆盖历史结论。完成后按第 3、4 步评分。

## 文件角色

- `full_official_prompt.py`：本次 500 题实验的推理入口，不读取金标 SQL。
- `pilot.py`：模型客户端、SQL 抽取、只读执行与早期 20 题调试工具。
- `grade_full.py`：本地逐题评分和官方格式导出。
- `summarize_full.py`：分组统计及精确配对检验。
- `official_per_item.py`：调用上游评分器做逐题复核；本仓库没有归档那次复跑的逐题产物。
- `selection.json`：早期 20 题选择清单，供排除调试题的敏感性分析使用。
