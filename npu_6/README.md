# npu_6: seeded, grain-refined and bottleneck-guided search

This isolated version builds on `npu_5_astra_trial/`. It leaves the official files, fifth version and Astra trial untouched. Read `算法设计说明.md` for the solver design and limitations.

Each available v4, v5 and Astra plan is checked and rescored by the current official evaluator. Large graphs first evaluate the fourth-version coarse grid and add the three fine grains around the best coarse official score. The Astra multi-start search then spends the remaining official-evaluation budget. A, B and L2 each optimize their own objective. The B job stores the top four plans and their hashes so an interrupted run can restore the whole pool for L2. The L2 paired-B run consumes one L2 evaluation from the same budget.

A candidates also pass the official lightweight Task-order DAG check before full simulation. Counters distinguish unique candidates considered, static rejections, official evaluator calls, valid official results and official failures. `official_evaluations` alone is capped by `--small-budget`/`--large-budget`.

## Pilot command (PowerShell from repository root)

```powershell
python .\npu_6\run.py --cases case_009 --cores 2 --workers 1 --small-budget 72 --large-budget 16 --v4-run .\多核调度_第四版\runs\20260923_141723_v4 --v5-run C:\Users\lin\Documents\Codex\2026-09-23\wqo\outputs\npu5_full_review\20260923_230843_05289c --astra-run C:\Users\lin\Documents\Codex\2026-09-23\wqo\outputs\npu5_astra_seeded\20260923_233134_8ac896 --no-plots
```

Use the paths to the actual completed/interrupted source runs available on the machine. A partial source run contributes only configurations whose job and plan files are both present and marked `ok`. `--run <directory>` resumes an existing npu_6 run only when code, official inputs and all used seed plan/job hashes match the manifest.

## Verification

```powershell
python -m unittest discover -s .\npu_6\tests -v
python .\npu_6\verify.py <run-directory> --replay
```

Only compare final cycle counts after representative official replay passes. A pilot with reduced budgets verifies implementation and safety properties; it does not establish a full-100-case algorithm improvement.


本阶段验证结果和适用限制见 [验证记录.md](验证记录.md)。
