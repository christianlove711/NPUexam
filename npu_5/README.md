# npu_5：三问官方评估驱动的多核调度搜索

`npu_5` 只新增代码，不改题目提供的 `code/`、`data/`，也不覆盖第四版运行记录。入口 `run.py` 为场景 A、B 和只读 L2 分别生成候选，并仅用官方评估器决定最终方案。所有最终方案都使用题目要求的 `node_to_subgraph`、`core_schedules` 两字段。

## 为什么改变搜索

第四版全量结果显示，场景 A 的五核方案中仍有 17/100 个只用一个核；`case_016` 和 `case_024` 在 2～5 核下 Makespan 相同。进一步缩小固定块会增加跨 Task 搬运，不能靠“越细越好”解决。场景 B 的 2～5 核平均加速比为 1.77、2.34、2.83、3.23，已普遍并行，但固定拓扑序和一次粒度搜索仍限制分核。L2 最终方案大多数直接沿用 B 方案；本版增加 L2 自己的候选和局部搜索，同时保留固定 B 方案的无 L2／有 L2 配对指标。

新增搜索包括：第四版固定粒度候选、已有的关键路径／复用／内存压力拓扑序与通信感知切点、拆分／迁核／合并局部修改。代理量只决定尝试顺序；每个入选方案都必须通过官方合法性检查和该场景的完整模拟。首要目标是最小 Makespan，平局时取更少额外 COPY 字节。第四版方案在当前环境重新官方评估后作为保底。算法不保证全局最优。

## Windows 运行

保留仓库相对目录结构，尤其是 `code/`、`data/`、`多核调度_第一版/`、`多核调度_第四版/`（含完整 `runs/20260923_141723_v4`）和 `npu_5/`。在项目根目录的 PowerShell 中运行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\多核调度_第四版\requirements.txt
.\.venv\Scripts\python.exe .\npu_5\run.py --cases case_001 case_019 --cores 2 5 --workers 2 --small-budget 12 --large-budget 12 --no-plots
.\.venv\Scripts\python.exe .\npu_5\run.py --workers 6
```

最后一条是正式全量运行，默认每组 5000 个及以下非 COPY 操作最多 72 次、其他图最多 44 次新的官方评估。提高 `--small-budget`、`--large-budget` 可扩大搜索，但不会保证成绩继续提高；减少 `--workers` 可降低内存压力。程序打印 `RUN=...`，该目录含 `manifest.json`、逐组 `jobs/` 和 `plans/`、三问 `summary.csv`、`reports/aggregate.csv`、`reports/v4_v5_comparison.csv` 和图表。

每个用例／核数组合在独立进程中运行，默认上限为 120 分钟；可用 `--timeout-minutes` 调整。超时任务会记录为失败，已完成的组合保留，使用 `--run` 续跑。L2 固定 B 方案的配对评估为必需步骤，在 L2 候选预算之外另执行一次。

中断后使用相同的 `--cases`、`--cores`、预算参数及 `--run` 续跑，例如：

```powershell
.\.venv\Scripts\python.exe .\npu_5\run.py --run .\npu_5\runs\已有运行目录 --workers 6
.\.venv\Scripts\python.exe .\npu_5\verify.py .\npu_5\runs\已有运行目录 --replay
.\.venv\Scripts\python.exe .\npu_5\export.py .\npu_5\runs\已有运行目录 .\npu_5\submission
```

续跑检查数据、固定配置和相关源码的哈希；源码或输入变化须建立新运行。`verify.py` 对全部保存方案做官方格式校验，`--replay` 另对代表方案重新运行官方模拟器。`manifest.status=complete`、`reports/validation.json` 为 `ok` 且 `reports/failures.csv` 无数据行，才是本次范围内的完整结果。

`export.py` 按 `submission/problem_1/2core/case_001_multicore_res.json` 等路径导出。各核数放在不同目录，避免同名用例互相覆盖；每个文件只有题目规定的两项顶层字段。

## 指标说明

问题一、二的 1 核点为 1；多核加速比为同用例官方单核 Makespan 除以多核 Makespan。问题三固定 **npu_5 的 B 方案**分别评估无 L2 和有 L2，报告 `T_B/T_L2`；另行选出的 L2 最终方案成绩不能当作纯 Cache 收益。`added_copy_bytes` 是官方逻辑 COPY 增量，不等于扣除 Cache 命中后的物理 DDR 流量。
