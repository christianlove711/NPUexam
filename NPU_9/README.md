# NPU_9：复用已有结果、限时增量优化

此版针对 2026-09-24 晚上的未完成实验。`NPU_8` 和原始运行目录没有改动。官方 `code/`、`data/config.txt` 和题目评分规则没有改动。

**目标 4.0 / 4.5 / 4.5 尚未验证达到。** 已有完整历史结果加上本次少量官方验证后的五核均值为 **3.680987 / 4.053822 / 4.195588**。这些可复用结果已经随 `seedpack/` 打包，不需要重跑昨晚的基线和全部旧候选。新搜索只接受官方评分更好的方案。

## 先运行哪条

在仓库根目录、`npu312` 环境执行。急着确认五核均值，建议先跑全部 100 个 case 的五核：

```powershell
python -u .\NPU_9\run.py --cores 5 --workers 12 --plots
```

这是三个问题、100 个 case、仅五核。不是 2—5 核全量。屏幕会输出 `RUN=...`，保留该目录。

**自己一台电脑运行三个问题、100 个 case、2—5 核完整实验：**

```powershell
python -u .\NPU_9\run.py --workers 12 --plots
```

默认核数顺序是 5、2、3、4，先产出五核结果。`workers` 默认就是 12，表示 CPU 工作进程数，不是模拟 NPU 的核数。

如果已经单独跑完五核，可以只补 2—4 核，随后合并两个目录：

```powershell
python -u .\NPU_9\run.py --cores 2 3 4 --workers 12 --plots
python .\NPU_9\merge_runs.py "五核运行目录" "二至四核运行目录" --output ".\NPU_9\runs\all_cores_merged"
```

## 两台电脑分开运行

两台电脑复制同一份 `NPU_9`、`code`、`data`。必须带 `NPU_9/seedpack`；不需要复制 `NPU_8/runs`、`npu_5/runs` 或 `NPU_9/analysis`。使用相同 Python 3.12 环境。换盘符、用户名不影响运行。

**孟凡罡电脑：问题一全量，问题三 case_051—case_100，及这部分问题三需要的问题二配对。**

```powershell
python -u .\NPU_9\run.py --machine 1 --workers 12 --plots
```

**陈卓钰电脑：问题二全量，问题三 case_001—case_050。**

```powershell
python -u .\NPU_9\run.py --machine 2 --workers 12 --plots
```

若两边都先只看五核，在以上两条后面都加 `--cores 5`。需要完整 2—5 核则不加。

把两份完整 RUN 目录复制到同一台电脑后：

```powershell
python .\NPU_9\merge_runs.py "孟凡罡电脑RUN目录" "陈卓钰电脑RUN目录" --output ".\NPU_9\runs\merged_two_machines"
python .\NPU_9\verify.py ".\NPU_9\runs\merged_two_machines"
python .\NPU_9\export.py ".\NPU_9\runs\merged_two_machines" ".\NPU_9\submission"
```

输出目录必须尚不存在。合并会核对官方代码、输入、配置、基线、源码和种子包身份。

**不会用两台电脑的实际运行秒数当作加速比基准。** 加速比使用同一图的官方单核 cycles / 官方多核 makespan。CPU 型号、后台负载主要影响搜索能完成多少候选；限时搜索可能使两边得到不同的 B 方案。合并因此固定保留所选 L2 来源的整组 B/L2 配对，不会把另一台更快的 B 分母直接拼上去。若放弃了另一个更快的 B，会写入 `reports/merge_choices.json`。这可能牺牲该个 B 的最佳值，但保证问题三配对成立。

## 大 case 单独跑、只精修需要的部分

新建一次只含大 case 的五核实验：

```powershell
python -u .\NPU_9\run.py --size large --cores 5 --workers 12 --large-workers 2 --large-budget 8 --large-seconds 180 --large-eval-seconds 60
```

只跑指定 case，例如 14 和 76：

```powershell
python -u .\NPU_9\run.py --cases 14,76 --cores 5 --workers 2 --large-budget 8
```

默认保留已经达到 `核数 × 0.95` 加速比的强结果。如确实想继续打磨这些 case，再加 `--polish-strong`。例如 case_014 五核 A 已经约 4.81，case_076 五核 A 已经约 5.02，默认不会继续为它们堆叠模拟次数。

在**本版同一 RUN 目录**上增加一次精修预算，可以复用该 RUN 的新增候选缓存：

```powershell
python -u .\NPU_9\run.py --run "本版RUN目录" --refine --cases 14,76 --cores 5 --large-budget 8 --large-seconds 180 --workers 2 --polish-strong
```

`--refine` 才会重新开启已完成的搜索；预算是这次额外允许的新评估数。未加 `--refine` 的续跑使用原目录保存的范围和搜索参数，不会把命令行新参数悄悄套到旧结果上。源码或种子包变化时会拒绝续跑，应新建 RUN。

只优化问题一可以加 `--scenes A`；只优化问题二、三可以加 `--scenes B L2`。新建 L2 运行会自动带上 B 配对。

## 时间与候选预算

| 项目 | 默认值 |
|---|---:|
| CPU workers | 12 |
| 大于 20,000 个非 COPY 算子的重任务并发上限 | 2（`--large-workers`） |
| 小 case（非 COPY 算子 ≤ 5,000）新评估数/场景 | 12 |
| 大 case 新评估数/场景 | 6 |
| 小 / 大 case 每场景搜索时间预算 | 60 / 150 秒 |
| 小 / 大 case 单次官方评估硬超时 | 15 / 60 秒 |
| L2 必要配对 | 每次处理最多额外 1 次评估 |

缓存命中不消耗新评估次数。三个场景在每个 case/core 任务中依次执行。场景时间是搜索预算，保存与候选生成可能略超；外层任务还有 `3 × 场景秒数 + 90` 秒的兜底终止，默认大 case 为 9 分钟，不再是 180 分钟。超时保留已存最好解，记录错误，终止所属评估进程树。暂停后 Ctrl+C 可能还需等待汇总检查。

这些是限制，不是全量耗时承诺；机器负载、图规模、实际候选耗时都会影响总时间。不要直接沿用旧版的 `--small-budget 120 --large-budget 80 --timeout-minutes 180`。

`status=complete` 表示范围内结果和 B/L2 配对完整合法，不表示尝试了无限搜索空间、所有候选都成功，或者达到目标均值。请同时查看 `stop_reason`、`errors`、`official_evaluations` 和 `reports/process_failures.json`。

## 续跑、检查、导出

```powershell
python -u .\NPU_9\run.py --run "屏幕输出的RUN目录" --workers 12
python .\NPU_9\verify.py "RUN目录"
python .\NPU_9\export.py "RUN目录" ".\NPU_9\submission"
```

仅重新统计并画图：

```powershell
python .\NPU_9\run.py --run "RUN目录" --report-only --plots
```

仅整理已有完整结果、不开展新候选搜索：

```powershell
python .\NPU_9\run.py --assemble-only --workers 12
```

`--assemble-only` 可能补做缺失的同 B 方案 L2 配对评估；不会从头重跑基线。后续想在其上优化，使用 `--refine`。

主要结果：`reports/aggregate.csv`、各问题 `summary.csv`、`reports/validation.json`。五核全量均值必须检查 `cores=5` 且 `count=100`；部分 case 的均值不能直接替代全量。问题三优化加速比和同 B 方案纯 Cache 增益分别报告，不能混用。

开发验证、原始诊断数据在 `analysis/`；开发验证目录记录的是当时源码，只作为证据，不建议拿来续跑。正式实验使用上面的命令创建新 RUN。
