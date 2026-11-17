# BIRD Mini-Dev data provenance

- Question release: BIRD Mini-Dev 2026 update, Hugging Face revision `5b256b5a9c17478f06ed6912612e9a14d17065c8`.
- Source: https://huggingface.co/datasets/birdsql/bird_mini_dev
- SQLite question file: `mini_dev_sqlite_2026.jsonl`, SHA-256 `926af2a0d1b47e5d0d15a7aa8a34d4721007f4550a881555b6a89496b54a2606`.
- Manifest: `manifest_2026.json`, SHA-256 `66ddb0e39e4ee6a443abff8e8f915674ec1602a36e9f1c763f4750414bcf4611`.
- SQLite databases and descriptions: extracted from the BIRD official `minidev.zip` at https://bird-bench.oss-cn-beijing.aliyuncs.com/minidev.zip ; archive MD5 `7beb6dab11e65f0fde563e644a1ea319`. MySQL/PostgreSQL SQL dumps were not extracted. The archive was removed after checksum verification and extraction.
- Count: 500 question IDs (0 through 499), 11 SQLite databases. Each database passed a read-only execution smoke test using one current gold SQL query.
- The 2026 JSONL contains `SQL` and `evidence` fields. Gold SQL is evaluator-only. The public evidence field is a separate optional input condition, never a substitute for independently collected human feedback.
- Official fixed-prompt SQLite baseline input: `official_baseline_input_sqlite_2026.jsonl`, SHA-256 `b060ee9cb69be62a052f5e40367afeb2cf08f345f15ca318fd9a0f9bf52d53ef`; verified against the official validator constant.
