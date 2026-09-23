# npu_5：完整独立第五版

本目录包括求解器、官方评估适配、批量运行、报告、核验和方案导出。运行时只需要同级的官方 `code/`、`data/`，不导入或读取第一至第四版。官方文件保持原样。第五版从代码重新求解，不捆绑旧版历史方案，因此没有逐例优于第四版的预先保证。

## 目录和方法

- `graph.py`：收缩 COPY 链并构造带通信字节代理量的操作 DAG。
- `fixed.py`、`partition.py`、`solver.py`：固定粒度、不同拓扑优先级、通信感知切分、场景化分核、拆分／迁核／合并／重排搜索。代理分数仅排序候选。
- `official.py`：调用官方 A、B、L2 模拟器；按 `(Makespan, added_copy_bytes)` 选优。每个候选先经过官方方案结构校验，再执行完整模拟。
- `solve.py`：单图生成题目要求的两字段方案。
- `run.py`：100 个官方图的单核基准及三问 2～5 核批量运行，逐组保存、断点续跑和超时控制。
- `reports.py`、`verify.py`、`export.py`：图表、核验和按场景／核数导出。

问题三将**同一份最终 B 方案**分别在无 L2 和有 L2 的官方模拟器中评估，`paired` 及 `paired_l2_speedup` 表示纯 Cache 配对结果；`selected` 是另外搜索的 L2 专用方案。`added_copy_bytes` 是官方逻辑额外搬运量，不是 L2 扣除命中后的物理 DDR 字节。

## Windows PowerShell

在仓库根目录运行。Python 3.12 可用；仅绘图需要 Matplotlib。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\npu_5\requirements.txt
.\.venv\Scripts\python.exe .\npu_5\solve.py .\data\case_019.json --cores 2 --scene A --budget 12 --output .\npu_5\case_019_A_2core.json
.\.venv\Scripts\python.exe .\npu_5\run.py --cases case_019 --cores 2 --workers 1 --small-budget 8 --large-budget 8 --no-plots
.\.venv\Scripts\python.exe .\npu_5\run.py --workers 6
```

最后一条为完整运行：100 例单核，A／B／L2 各 400 条多核记录。默认并发 6、每组超时 120 分钟、小图候选预算 72、大图 44；可用 `--workers`、`--timeout-minutes`、`--small-budget`、`--large-budget` 调整。控制台打印 `RUN=...`，结果保存在 `npu_5/runs/`。本机未运行全量，目录只有实际运行后才会生成全量成绩。

中断后保留原目录，用下面命令继续。续跑检查所选用例、核数、预算、配置、输入数据及第五版与官方源码的 SHA256；内容变更须另开一轮。

```powershell
.\.venv\Scripts\python.exe .\npu_5\run.py --run .\npu_5\runs\你的运行目录 --workers 6
.\.venv\Scripts\python.exe .\npu_5\verify.py .\npu_5\runs\你的运行目录 --replay
.\.venv\Scripts\python.exe .\npu_5\export.py .\npu_5\runs\你的运行目录 .\npu_5\submission
```

`manifest.json` 的 `status=complete`、`reports/validation.json` 为 `ok`、`reports/failures.csv` 无数据行，才表示所选范围完成。只有 100 条单核基准、每问 400 条多核记录、完整 1～5 核图表都存在时，`full_experiment_complete` 才为 `true`。`--replay` 重算每问、每核数的代表方案，以及问题三固定 B 方案的 Cache 配对。导出格式如 `submission/problem_1/2core/case_001_multicore_res.json`，每个文件只有 `node_to_subgraph`、`core_schedules` 两字段。

`reports/aggregate.csv` 与 `problem_*_speedup.png` 给出 1～5 核逐例等权平均加速比。各问 `summary.csv` 给出逐例 Makespan、额外搬运量；问题三还给出 Cache 配对 Makespan、命中率与同方案加速比。`problem_3_cache_gain.png` 单独显示纯 Cache 收益。
