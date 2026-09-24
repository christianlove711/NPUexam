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

## 只运行问题一

问题一搜索先保留原有候选与局部搜索结果，再增加最多 16 次官方评估，优先合并通信量大的同核 Task，然后尝试迁核与拆分。如果最优方案未用满核心，或相对单核基准的加速比不超过 1.2，还会额外评估独立依赖分量分核方案。最后对每个 A 场景加入有限的图感知候选：按 DDR 大边界合并 Task，并在长图上融合无分叉、无汇合的直线链。旧最优始终保留；默认小图最多 96 次、大图最多 68 次官方候选评估。只跑问题一时，批量程序只计算单核基准及 A 场景，不运行 B／L2。全量运行在项目根目录执行：

```powershell
& "C:\Users\chris\miniconda3\envs\npu312\python.exe" -u .\npu_5\run.py --scene-a-only --workers 6
```

运行会打印 `RUN=...`。中断后把该路径填入 `--run` 即可续跑；`manifest.json` 中 `problem_1_complete=true` 表示 100 例、2～5 核全部完成。

## 问题一的增量优化实验

`improve_a.py` 读取第四版和第五版已经保存的问题一方案，先用官方评估器重算两个起点，再优先尝试合并同核 Task、迁移 Task、交换顺序和拆分。实验结果写入独立目录，不修改历史运行记录。`compose_a.py` 从两版与实验结果中按官方 Makespan 逐例择优，输出 400 个问题一方案及 `summary.csv`。此方法使用已知测试图上的历史结果；新图应运行 `solve.py` 搜索，不能直接套用历史方案。

```powershell
& "C:\Users\chris\miniconda3\envs\npu312\python.exe" .\npu_5\improve_a.py --cases case_093 case_078 case_022 case_007 --budget 20
& "C:\Users\chris\miniconda3\envs\npu312\python.exe" .\npu_5\improve_a.py --cases case_084 case_036 --budget 60 --output .\npu_5\a_refinement_highspeed
& "C:\Users\chris\miniconda3\envs\npu312\python.exe" .\npu_5\compose_a.py
```

生成方案位于 `npu_5/a_optimized/plans/`。例如五核第 84 例是 `case_084_5core.json`，提交官方评估器时可将其作为方案文件参数传入。`compose_a.py --verify` 可对选出的 400 个方案全部做官方重放，耗时较长。

## 问题一的定向诊断与限额实验

`diagnose_a.py` 只读取已保存的五核 job 和方案，不调用官方模拟器。输出每例的活跃核心、Task 数、搬运下界占比、候选成功/失败次数和耗时。瓶颈类别是诊断标签，不是全局最优性的证明。

`target_a.py` 从一次已完成的问题一运行读取单核基准和已评估的最优方案；先核对图、配置、官方评估器源码及方案哈希，然后只评估新方案。默认策略尝试独立分量、单 Task 保底、粗粒度切分以及局部改动；不允许把种子方案覆盖为更差的结果。提交给模拟器前会筛掉 Task 顺序中的依赖环。每例使用 `--max-evaluations` 和 `--case-seconds` 限额；官方模拟在独立子进程中执行，到期可中止当前模拟并保留已知最好方案。时间限额涵盖搜索和评估；读取与检查种子文件在搜索计时前完成。新结果写入 `npu_5/targeted_runs/`，不会改动种子运行。

另有两种定向实验策略：`--strategy affinity` 保留大张量依赖链、合并轻量归约链并对并行分支分组；`--strategy chains` 只融合无分叉、无汇合的直线依赖链。它们位于 `affinity_a.py`；默认 `run.py` 已接入有限候选，定向运行仍可单独设置更大的评估上限。新策略都通过图结构决定分组，不按 case 编号硬编码方案。

在仓库根目录示例：

```powershell
& .\.venv\Scripts\python.exe .\npu_5\diagnose_a.py --run .\npu_5\runs\20260924_102338_e258fd --output .\npu_5\diagnostics\problem_1_20260924.csv
& .\.venv\Scripts\python.exe -u .\npu_5\target_a.py --seed-run .\npu_5\runs\20260924_102338_e258fd --scene-a-only --cases case_016 case_022 case_024 case_049 case_051 case_084 --cores 5 --max-evaluations 8 --case-seconds 300
```

定向运行的 `manifest.json` 中 `problem_1_complete=false`，它只代表选定 case 的实验，不能替代 100 例、2～5 核的平均加速比。中断后用相同参数加 `--run <打印的 RUN 路径>` 续跑；源码、输入或限额改变时需新建运行。
