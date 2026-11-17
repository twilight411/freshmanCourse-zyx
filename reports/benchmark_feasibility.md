# Benchmark feasibility for a first Text-to-SQL experiment

> **Provisioning update, 2026-11-04 Beijing time:** This document records an
> earlier feasibility snapshot. Its statements that BIRD databases were not
> downloaded, were unverified, or blocked the pilot are superseded. The
> 800,943,648-byte official `minidev.zip` was downloaded, its MD5 was verified
> as `7beb6dab11e65f0fde563e644a1ea319`, and the 11 SQLite databases plus
> 75 description files were extracted on the server. The archive was then
> removed to recover disk space. One gold query per database executed, and
> the official evaluator returned 100% on an 11-example gold-as-prediction
> plumbing check. That check measures evaluator wiring, not model accuracy.
> The current dataset, database paths, and hashes are in `data/PROVENANCE.md`
> on the server. Sections 1, 2, 6, 7, 11, and 12 below retain historical
> resource estimates; do not use their old provisioning status as current.

**Assessment date:** 2026-11-04 (provenance captured at `2026-11-03T18:08:43Z`).  
**Pinned versions:** [`reports/source_versions.json`](source_versions.json).  
**Measured accuracy:** none. No model was called, no inference was run, and no benchmark score was computed. Numbers in official papers or cards are deliberately not repeated as findings.

## 1. Decision

Use **BIRD Mini-Dev 2026, SQLite only**, as the first and only initial benchmark:

1. a 20-question pipeline pilot over all 11 databases;
2. a later 500-question confirmatory run.

BIRD is the best fit because the annotations are public and content-addressed, the evaluator is local and simple, and SQLite needs no external database service. It is **not executable today** under the stated download/storage rules: the database material is larger than the 400 MB allowance and the host currently has only 226 MiB of headroom above the 4 GiB free-space floor. The pilot is feasible methodologically and on 4 GB RAM; data provisioning is the blocking gate.

BEAVER is not the first experiment: its query and table datasets are gated, its database is MySQL, its default retrieval stack expects an 8B embedding model or an external API, and its "hints" are explicitly **oracle hints**, not deployable human feedback. Spider 2.0 is not the first experiment: only 135 of 547 Lite tasks use local SQLite, all 547 Snow tasks require Snowflake, and DBT is a separate DuckDB/dbt code-agent task.

## 2. Pinned primary sources

| Source | Pin | Evidence inspected |
|---|---|---|
| BIRD Mini-Dev repository | [`1c49a375e76c471e218564da3f4617513d33e371`](https://github.com/bird-bench/mini_dev/tree/1c49a375e76c471e218564da3f4617513d33e371) | README, evaluator, runner, requirements |
| BIRD Mini-Dev dataset | [`birdsql/bird_mini_dev@5b256b5a9c17478f06ed6912612e9a14d17065c8`](https://huggingface.co/datasets/birdsql/bird_mini_dev/tree/5b256b5a9c17478f06ed6912612e9a14d17065c8) | dataset card, manifest, SQLite annotation JSONL, baseline prompt template/validator |
| BIRD SQLite data archive | `minidev.zip`, ETag `7BEB6DAB11E65F0FDE563E644A1EA319`, Content-MD5 `7beb6dab11e65f0fde563e644a1ea319` | HEAD metadata only; **not downloaded** |
| BIRD SQLite image | tag `20261009`, index digest `sha256:f5e2746e284fdf4bc5d7a168a42d951bdf3d429d6db91b9928308e8261d0b75e` | OCI manifest metadata only; **not downloaded** |
| BEAVER repository | [`5a6509f6252a65265d3f4e38522c75823d128a62`](https://github.com/beaverbench/beaver/tree/5a6509f6252a65265d3f4e38522c75823d128a62) | README, data downloader, evaluator, hint construction |
| BEAVER paper | [arXiv:2409.02038v3](https://arxiv.org/abs/2409.02038), 2026-05-13 | oracle-hint methodology |
| BEAVER query/table datasets | `beaver-query@3c8eb607ee2b7709ad4b1d53243e04af1b9eda4b`, `beaver-table@86f2c4ec45990ac30677dfdfec9826f28171000a` | gated metadata only; **not downloaded** |
| Spider 2.0 repository | [`cafb867313aab4e674652054198f383cf4018943`](https://github.com/xlang-ai/Spider2/tree/cafb867313aab4e674652054198f383cf4018943) | README, variant docs, evaluators, task manifests |

No database archive was fetched by this inspection. The only BIRD annotation payload fetched was the 471,159-byte SQLite JSONL, whose SHA-256 matches BIRD's own manifest (`926af2a0…`); small Spider 2.0 task manifests were fetched as well. No gold SQL value was printed, used to choose the pilot, or used to write any hint.

## 3. Benchmark comparison

| Benchmark / variant | Public size and local data | External DB or cloud | Access | Primary metric | Local fit |
|---|---|---|---|---|---|
| **BIRD Mini-Dev SQLite** | 500 questions; 11 SQLite DBs; 1 annotation JSONL | **None**; SQLite local. LLM inference still needs an external API or local model | Public HF annotations; DB archive/image public | BIRD EX; R-VES and soft F1 are beta extras | Good after data gate; no cloud DB |
| **BEAVER** | 4 query splits; 262 MB gated DB ZIP; MySQL | **Local MySQL** required; no cloud DB. Default retrieval needs GPU/8B model or OpenRouter; judging needs an LLM API | HF **gated auto**; MySQL credentials | Execution accuracy plus five subtask scores | Possible but not first: gated, MySQL, resource-heavy retrieval |
| **Spider 2.0-Lite** | 547 tasks: 135 local SQLite, 214 BigQuery, 198 Snowflake per docs | **BigQuery and Snowflake** for full run; local SQLite only for 135 | Public task files; Google Drive local DB; GCP service account; Snowflake account | Task-specific result-table match; score reported over all 547 | Only 135/547 is fully local |
| **Spider 2.0-Snow** | 547 tasks | **Snowflake only** | Access request, MFA, programmatic token | Task-specific result-table match | No; cloud required |
| **Spider 2.0-DBT** | 68 tasks verified (docs also say 69) | **None**: DuckDB/dbt local, Docker for the agent | Public repo; Google Drive downloads; Docker | Task-specific number/string/table/DuckDB match | Locally reproducible in principle; not validated here |

All six require either an external model API or a model that cannot fit in this 4 GB host. "Locally reproducible" below refers to the **database and evaluator**, not to local LLM inference.

## 4. Evaluator commands

### BIRD Mini-Dev SQLite

The repository runner is a template, not a turnkey script:

- it hardcodes `num_cpus=16` and `meta_time_out=30.0`;
- it expects relative paths such as `../sqlite/dev_databases/` and `../sql_result/...` that are not present in a fresh clone;
- `evaluation_utils.py` imports `psycopg2` and `pymysql` unconditionally, so those small drivers are needed even for SQLite.

After the DB snapshot has been provenance-checked, run the official evaluator directly with two workers:

```bash
cd /home/codex-runner/workspaces/text2sql
python3 -u sources/mini_dev/evaluation/evaluation_ex.py \
  --db_root_path data/bird_mini_dev_db/dev_databases/ \
  --predicted_sql_path work/bird_pilot20/predictions.json \
  --ground_truth_path work/bird_pilot20/gold.sql \
  --diff_json_path work/bird_pilot20/rows.jsonl \
  --num_cpus 2 \
  --meta_time_out 30 \
  --sql_dialect SQLite \
  --output_log_path work/bird_pilot20/eval_ex.txt
```

Required input formats:

- `predictions.json`: JSON object whose values are `SQL<TAB>----- bird -----<TAB>db_id`. The evaluator does **not** use that parsed `db_id`; it pairs predictions to ground truth **by position**.
- `gold.sql`: one line per question, `SQL<TAB>db_id`, in exactly the same order.
- `rows.jsonl`: the matching 20 annotation rows in the same order; the evaluator uses only their `difficulty` values.

Because pairing is positional, build all three files from one ordered `question_id` list and assert the IDs before running. A keyed implementation is safer than reusing the runner's order assumptions.

Documented evaluator semantics: EX is 1 only when the **set** of predicted result rows equals the set of ground-truth rows. It ignores row order and duplicate multiplicity. R-VES is a timed efficiency metric and the README recommends repeated measurements; on a shared 2-vCPU host it should not be the headline metric. Soft F1 is marked beta.

### BEAVER

Documented coarse and fine-grained evaluation:

```bash
cd sources/beaver/eval
python evaluate_ex_acc.py \
  --dataset dw \
  --input_dir unified-output/<method>/<run_name>

python evaluate_subtasks.py \
  --dataset dw \
  --model <judge-model> \
  --num_workers 2 \
  --input_dir unified-output/<method>/<run_name>
```

Generation is driven by `eval/<method>/run.sh --model <model> --dataset <dataset> --setting {0,1,2}`. Setting 0 is the no-gold-hint condition; settings 1 and 2 are oracle-hint conditions described below. The evaluator requires `MYSQL_HOST`, `MYSQL_USER`, and `MYSQL_PASSWORD` (or a credentials file) and uses a 10-second query timeout. Its coarse comparison checks row sets and column count, ignoring column names and duplicate rows; in this revision it does not actually try column permutations despite the docstring.

### Spider 2.0-Lite

```bash
cd spider2-lite/evaluation_suite
python evaluate.py \
  --result_dir <predicted_sql_directory> \
  --mode sql \
  --max_workers 2 \
  --timeout 60
```

or score prepared CSV outputs with `--mode exec_result`. Place `bigquery_credential.json` and `snowflake_credential.json` in that directory and install the local SQLite DBs. The evaluator reports both a score over submitted IDs and a "Real score" over all 547; a local-only submission is therefore a **partial** benchmark, not a full Lite result.

### Spider 2.0-Snow

```bash
cd spider2-snow/evaluation_suite
python evaluate.py \
  --result_dir <predicted_sql_directory> \
  --mode sql \
  --max_workers 2
```

The Snow README says `--model sql`; the code and Lite README use `--mode`. The code is authoritative. A `snowflake_credential.json` and cloud access are mandatory.

### Spider 2.0-DBT

```bash
cd spider2-dbt/evaluation_suite
python evaluate.py \
  --result_dir <submission_directory> \
  --gold_dir gold
```

This variant is local DuckDB/dbt in principle, but the evaluation module still imports BigQuery helpers, so cloud client libraries may be needed as imports even when a task executes locally. The source also needs Docker, `gdown`, project setup, and LLM access for the agent.

## 5. Public evidence versus oracle answer material

This distinction is a fire-wall, not a naming preference.

| Material | Origin | Allowed at inference? | Permitted use |
|---|---|---|---|
| `question`, `db_id`, public schema | Benchmark input | **Yes** | Prompt construction |
| BIRD `evidence` | Benchmark-supplied expert annotation; present in 365/500 current SQLite rows | **Yes**, as benchmark input | Keep constant in the main comparison; ablate only as a separately labelled experiment |
| Database catalog, descriptions, foreign keys, value statistics | Database/schema | **Yes**, if computed without gold | Schema guidance, retrieval, pruning |
| Predicted SQL's own error or result | System-generated | **Yes** | Execution-guided repair |
| `SQL` field | Oracle gold SQL | **No** | Held-out scorer only, after predictions are frozen |
| Execution result of gold SQL | Oracle | **No** | Scorer only |
| BEAVER settings 1/2 hints | Gold-derived annotations | **No** for a deployment claim | Explicitly labelled oracle ceiling/diagnostic only |

BIRD's README calls `evidence` "oracle knowledge evidence". That means it is privileged annotation supplied with the task, **not** that it is gold SQL. It is still public task input and is included in BIRD's fixed baseline prompt. To avoid conflating annotation privilege with system capability, keep the official evidence input fixed across the main baseline/schema/repair arms, and run a no-evidence ablation separately if needed.

**Hard rule:** no prompt, retrieval index, schema hint, critic message, or repair feedback may be derived from `SQL` or from executing `SQL`. Execution-guided repair may execute only the system's candidate SQL. The scorer may read gold SQL only after the prediction file has been hashed and frozen.

Practical enforcement:

- split the annotations into `public_inputs.jsonl` (no `SQL`) and a separate oracle store keyed by `question_id`;
- run inference with no path/env variable pointing at the oracle store;
- hash `predictions.json` before invoking the evaluator;
- make the evaluator process the only reader of `gold.sql`;
- log every prompt and every repair message for audit;
- never use evaluator output to revise prompts inside the same run.

## 6. BIRD 20-question pilot

**Selection rule.** Use a hash-stratified sample, not the first 20 rows. From annotation revision `5b256b5a…`, compute

```text
SHA-256("birdsql/bird_mini_dev@5b256b5a9c17478f06ed6912612e9a14d17065c8|pilot20-v1|" + question_id)
```

Take the minimum-hash row from each of the 11 `db_id` groups, then the next nine lowest-hash rows overall. This guarantees all 11 schemas are exercised and avoids selecting on question wording, difficulty, or any gold outcome.

Pilot IDs:

```text
9, 49, 75, 94, 119, 155, 161, 168, 198, 218,
269, 310, 332, 367, 378, 432, 451, 461, 475, 480
```

Coverage from public metadata only:

- 11/11 databases;
- 13/20 with non-empty `evidence`, 7/20 without;
- 1 simple, 3 moderate, 16 challenging under the current labels.

The 20-question pilot is for plumbing: prompt rendering, SQL extraction, read-only database access, positional alignment, timeout handling, and audit logs. It is not an accuracy estimate and must not be used to choose prompts or hyperparameters from its score.

**Inputs.** Use BIRD's published fixed prompt for the baseline: `question`, `evidence` (when non-empty), and the pinned per-database schema. The official prompt template deliberately omits gold SQL; its validator asserts that. The prompt template SHA-256 is `62b75a14…`; the declared SQLite schema hash is `5176e324…`.

**Outputs.** Freeze one JSON prediction object per question, one SQL per question, and a run manifest containing model/version, decoding settings, prompt hash, annotation hash, and database digest. Do not execute an inference-time query that references the gold SQL.

**Pilot gate.** Proceed to the 500 run only if all 20 prompts render, all predictions parse, the evaluator completes with two workers, all 20 position/ID alignments pass, and the audit shows no oracle access. Do not set an accuracy threshold for the pilot.

### Later 500-question confirmatory run

- Same annotation revision and SHA-256.
- Same database image/digest or verified equivalent.
- Same frozen prompt template, model version, and decoding configuration.
- All 500 SQLite questions; no MySQL/PostgreSQL expansion in the first experiment.
- Pre-register EX as the primary outcome. Treat soft F1 and R-VES as secondary/beta and do not headline R-VES on a shared 2-vCPU host.
- Report paired uncertainty (for example, paired bootstrap over questions) and per-database counts. Do not compare against paper numbers as if they were rerun on this host.
- The pilot's 20 questions remain in the 500. The pilot must not be used for prompt or model selection; otherwise the confirmatory interpretation is weakened.

The 500 run still needs no cloud database. It does need the 11 SQLite files, roughly 1.49 GB uncompressed in the unverified tree currently present. The image is 352 MB compressed; the legacy archive is 801 MB compressed.

## 7. Resource and runtime estimate

These are arithmetic/resource estimates, **not measurements**.

| Item | Estimate / constraint |
|---|---|
| Host RAM | 4,105,363,456 bytes total (3.8 GiB); only 1,708,441,600 bytes (1.6 GiB) available at inspection; 2 GiB swap |
| CPUs | 2 |
| disk | 40 GB nominal, but 4,532,322,304 bytes (4.22 GiB) available; 226 MiB above a 4 GiB floor |
| BIRD archive | 800,943,648 bytes (764 MiB): exceeds the 400 MB budget and, separately, the current free-space headroom |
| BIRD Docker image | 352,065,205 bytes compressed (336 MiB): under 400 MB but pulling it would immediately cross the 4 GiB floor; extracted DBs are ~1.49 GB |
| BIRD SQLite evaluation | SQLite plus two Python workers should fit available RAM; no local model. Cap to 2 workers and keep per-connection cache modest. |
| 20-question eval time | Tens of seconds to a few minutes typically; arithmetic worst case at 30 s/query and 2 workers is about 5 minutes, excluding model latency |
| 500-question eval time | API generation dominates. At an assumed 5 s/call and 2-way concurrency, about 21 minutes just for calls; arithmetic worst case at 30 s/query and 2 workers is about 2.1 hours. No runtime is claimed. |
| BEAVER | 261,603,612-byte DB ZIP alone is larger than the current 226 MiB headroom; MySQL plus Python is tight in 1.6 GiB available RAM. Qwen 8B embedding/reranker cannot run locally. |
| Spider 2.0-Lite local 135 | Download size/hash is not published by the Google Drive link, so it cannot be certified against 4 GB RAM/40 GB disk without downloading; no attempt was made. |
| Spider 2.0-Snow | Database is remote; local cost is network/cloud, not SQLite storage. Current Snowflake access is also reported suspended in the repository news. |
| Spider 2.0-DBT | Local DuckDB, but Docker and dbt dependencies; unverified size. Not a first choice on a 4 GB host. |

### Current workspace note

An extracted `data/bird_mini_dev_db/` tree with 11 SQLite files totalling 1,493,287,936 bytes was already present and is **unverified**. It is not included in the provenance above, was not downloaded by this inspection, and must not be treated as a benchmark-valid database snapshot until its source and hashes are established. It explains part of the low free-space figure but is not relied on by this report.

## 8. Why BEAVER's gold hints are oracle settings

BEAVER's own paper is explicit. arXiv v3 refers to "all subtask annotations as **oracle hints**" and says that for table retrieval, "instead of giving the method top-k retrieved tables, we provide the **exact set of tables used in the gold SQL statement**." The repository implements the same split:

- `setting=0`: retrieved top-k tables, no gold hints;
- `setting=1`: **gold tables**, column mapping, and join keys;
- `setting=2`: setting 1 plus domain knowledge and subquery decomposition.

The prompt code literally emits `[Gold Tables]`, `[Schema Mapping Hints]`, `[Join Key Hints]`, and "Subquery Gold Questions". These are not estimates or hints inferred by a deployed assistant. They are complete, correct, per-question labels tied to the reference solution. Human annotators may have produced them, but **human annotation of the gold solution is not human feedback from a user interacting with an imperfect system**.

An honest human-feedback deployment would have:

- a user who does **not** know the gold tables, columns, join keys, or decomposition;
- partial, noisy, sometimes mistaken feedback;
- feedback conditioned only on the question, the system's visible SQL/result, and the UI;
- a real cost and latency;
- no guaranteed correctness.

BEAVER settings 1 and 2 have none of those properties: they reveal solution structure, are guaranteed relevant, and are unavailable without gold. They are useful as an **oracle-guided ceiling/diagnostic** to measure how much headroom is attributable to schema linking and decomposition. They are not evidence that a deployed human-feedback loop will achieve the same result. Label any such run `oracle_subtask_hints`, never `human_feedback`, and keep it out of the confirmatory comparison table.

If a human-feedback study is run later, blind participants to gold SQL and gold results. Show them the question, the schema context the system actually had, the system's SQL, and the system's own result/error. Record their messages verbatim. If an LLM simulates the user, call it **simulated feedback**, not human feedback.

## 9. Spider 2.0 local reproducibility

- **Lite:** only the 135 `local*` SQLite tasks are locally reproducible. The other tasks need BigQuery and/or Snowflake, and the evaluator's denominator remains 547. Full Lite is not local.
- **Snow:** not locally reproducible. It requires a Snowflake account, MFA, a role/warehouse, and a programmatic token. The repository news of 2026-08-12 says Snowflake evaluation-account access is currently disrupted.
- **DBT:** locally reproducible in principle as a DuckDB/dbt code-agent benchmark, with no cloud database. It requires Docker, two Google Drive downloads, project setup, LLM access, and enough disk for the dbt projects. It is a repository-level code task, not a drop-in replacement for a simple SQL-generation baseline. The current repository text says 69 examples, while both the task manifest and gold-evaluation manifest contain **68**; use 68 and record the discrepancy.

Documented counts also do not fully reconcile: the main README says 214 BigQuery, 198 Snowflake, 135 SQLite, while instance-ID prefixes give 205 BigQuery-like, 207 Snowflake-like, and 135 local. Use the task metadata and explicit `db` values, not the prefix counts, when constructing a local subset.

## 10. Recommended study order

Do not start with oracle hints or human feedback. The ladder should be:

### 1. Baseline (BIRD 20, then 500)
Use the exact fixed BIRD prompt with question, public evidence, and full pinned schema. No added hints. This measures the unassisted model+harness under the official task contract.

### 2. Schema guidance
Keep the question and evidence fixed. Add only schema-derived guidance: relevant table/column descriptions, foreign-key paths, value samples, or schema pruning computed from the database and documentation. **Never** use gold tables, gold columns, gold joins, or gold results. Compare against the baseline with the same model, decoding, and database snapshot.

### 3. Execution-guided repair
Execute the system's candidate SQL read-only, return only candidate errors or candidate results to the model, and allow a fixed small number of repair rounds (for example two). Never execute or reveal gold SQL during this loop. Log the original candidate, each repair message, and the final SQL. This arm answers whether local execution feedback helps beyond schema guidance.

### 4. Human-feedback study
Only after the automated ladder is reproducible. Use blinded human participants, a fixed interface, a fixed feedback protocol, and no access to gold SQL/results. Analyze feedback quality and noise, not just final EX. If the study is simulated with an LLM, label it as simulation.

BEAVER oracle-hint settings belong beside this ladder, not inside it: run them only as a clearly labelled diagnostic ceiling on a separate dataset, and do not claim that they represent human-feedback deployment.

## 11. Known gaps and risks

1. **BIRD data-version mismatch risk.** The 2024 archive predates the 2026 annotation revision; the `20261009` Docker tag also predates the annotation-release commit. No per-SQLite-file hash manifest is published at the pinned revision. Establish DB/annotation compatibility before scoring.
2. **Disk floor is already binding.** The host has 4.22 GiB free. Pulling the 336 MiB BIRD image would cross the 4 GiB floor; fetching the 801 MB archive is also over the 400 MB budget. Do not start either without operator action.
3. **BIRD README statistics are stale.** The current SQLite JSONL metadata has 8 simple, 32 moderate, and 460 challenging labels, not the README's old 30/50/20 description. Use the pinned rows, not prose statistics.
4. **Positional evaluator coupling.** BIRD pairs predictions and gold by list position and ignores the prediction's parsed `db_id`. A correct ID-keyed wrapper is necessary; otherwise a correct SQL can be scored against the wrong database.
5. **Public evidence is not gold SQL, but it is privileged annotation.** It is allowed task input, yet it can carry schema-linking information. Keep it fixed or ablate it; do not generate new evidence from gold.
6. **BEAVER's gated DB card was not accessible.** Its database setup instructions and LFS object hash could not be verified without authenticated access. The 262 MB ZIP is close to the current free-space headroom and is not usable now.
7. **Spider 2.0's local DB size and hashes are unpublished/unverified.** The Google Drive resources returned no content length. Do not infer that the 135-task local subset fits the resource budget.
8. **No accuracy was measured.** No baseline, schema-guidance, repair, human-feedback, or benchmark claim is made here.

## 12. Missing external access

- **BIRD data provisioning:** an approved SQLite database snapshot with verified provenance, or explicit approval to exceed the 400 MB/4 GiB limits and roughly 1.2–2.0 GiB of additional free space. Current free headroom is 226 MiB.
- **Model endpoint:** an API key/endpoint for the frozen model (or a model that can actually run in 4 GB; none is assumed), plus rate/cost approval.
- **BEAVER:** Hugging Face gated-dataset acceptance and authentication, MySQL 8, database setup credentials, an 8B embedding/reranker substitute (OpenRouter or GPU), and an LLM judge key.
- **Spider 2.0-Lite:** BigQuery service account and Snowflake account/credentials if the full benchmark is ever attempted.
- **Spider 2.0-Snow:** Snowflake participant account, MFA enrollment, and programmatic token; current access is disrupted.
- **Spider 2.0-DBT:** Docker, Google Drive downloads, Python project dependencies, and an LLM API key.
- **Human-feedback phase:** participants, consent/IRB review if applicable, and a blind protocol that never exposes gold SQL or gold results.

## 13. Next exact command

This command is safe under the current limits: it reads the already verified annotation file, writes only the 20 public inputs (no `SQL` field), and makes no network, database, or model call.

```bash
cd /home/codex-runner/workspaces/text2sql && python3 -I - <<'PY'
import hashlib, json, pathlib

src = pathlib.Path('data/mini_dev_sqlite_2026.jsonl')
want = '926af2a0d1b47e5d0d15a7aa8a34d4721007f4550a881555b6a89496b54a2606'
assert hashlib.sha256(src.read_bytes()).hexdigest() == want, 'annotation hash mismatch'

ids = {9, 49, 75, 94, 119, 155, 161, 168, 198, 218,
       269, 310, 332, 367, 378, 432, 451, 461, 475, 480}
rows = [json.loads(line) for line in src.read_text(encoding='utf-8').splitlines()]
selected = [{k: r[k] for k in ('question_id', 'db_id', 'question', 'evidence', 'difficulty')}
            for r in rows if r['question_id'] in ids]
assert len(selected) == 20 and len({r['db_id'] for r in selected}) == 11
assert all('SQL' not in r for r in selected)

out = pathlib.Path('work/bird_pilot20/public_inputs.jsonl')
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in selected),
               encoding='utf-8')
print(f'wrote {len(selected)} public inputs to {out}; SQL fields: 0')
PY
```
