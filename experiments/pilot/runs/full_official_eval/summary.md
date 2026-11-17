# BIRD Mini-Dev 2026: fixed-prompt paired run

The baseline sends BIRD's published fixed SQLite prompt unchanged. The structured arm inserts one checklist before its Output Format section. Both use DeepSeek `deepseek-flash`, temperature 0, thinking disabled, and max 4096 output tokens. The checklist was already explored on a 20-question pilot, so this is an exploratory validation, not a preregistered independent test. It contains no human feedback.

| Group | n | Baseline EX | Structured EX | Baseline only | Structured only | Delta (pp) | Exact paired p |
|---|---:|---:|---:|---:|---:|---:|---:|
| All 500 | 500 | 210/500 (42.00%) | 223/500 (44.60%) | 49 | 62 | +2.60 | 0.2546 |
| Excluding the 20 engineering-pilot questions | 480 | 198/480 (41.25%) | 209/480 (43.54%) | 49 | 60 | +2.29 | 0.3382 |
| simple | 8 | 5/8 (62.50%) | 5/8 (62.50%) | 0 | 0 | +0.00 | 1 |
| moderate | 32 | 14/32 (43.75%) | 17/32 (53.12%) | 2 | 5 | +9.38 | 0.4531 |
| challenging | 460 | 191/460 (41.52%) | 201/460 (43.70%) | 47 | 57 | +2.17 | 0.3776 |

Gold SQL executed successfully in 500/500 cases in the local scorer.
The official BIRD evaluator's aggregate outputs are in `baseline_official.txt` and `structured_official.txt`; compare these with the local counts before using the result.
Execution accuracy uses unordered set equality, matching the official scorer. A 500-question public benchmark result is not an independent held-out generalization estimate.
