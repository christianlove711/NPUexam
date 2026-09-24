# NPU_8：三问优化与双机全量实验

本版独立保存三个问题的求解器、批量程序、历史种子、双机分片、合并、绘图、核验和导出工具。运行只依赖本目录及仓库同级的 `code/`、`data/`。旧版目录和官方代码、配置、数据没有改动。

`reference/` 收录第五版 `20260924_161743_fb450c` 的问题一 400 个最终方案，以及 `20260923_220123_13c740` 的问题二、三各 400 个最终方案、精简成绩和来源 manifest，共 1,200 个种子。今天问题一全量 2/3/4/5 核平均加速比分别为 1.791620、2.479281、3.091124、3.561939。三问分别用对应官方评估器重评历史成绩，不能把 A 分数直接当成 B/L2 分数。种子缺失、重评失败或成绩变化会报错；成功结果保留同场景种子参与择优，因此问题二、三也有各自的历史成绩保底。

本版已根据两次全量历史结果再次修订。详细的 400 组问题一对比、400 组 B/L2 诊断和规则核查见 [全量分析与修订说明](全量分析与修订说明.md)。之前启动的 NPU_8 任务属于旧源码；此次需新开 RUN，不能在旧目录直接续跑修改后的算法。启动指令和双机划分保持不变。

## 单台电脑运行三个问题全量

在仓库根目录的 PowerShell 中激活环境，然后执行：

```powershell
conda activate npu312
python -m pip install -r .\NPU_8\requirements.txt
python -u .\NPU_8\run.py --workers 12 --small-budget 120 --large-budget 80 --timeout-minutes 180
```

不加 `--machine` 即在一台电脑上运行三个问题全部 100 个 case、2～5 核：100 个单核基准，以及 A/B/L2 各 400 组多核任务，共 1,200 组多核结果。所有结果保存在同一个打印的 `RUN` 目录中，不需要合并。单机全量与下方双机分片是两种可选运行方式。

单机完成后，将下面的占位路径替换为该次打印的 RUN 路径，直接核验和导出：

```powershell
python .\NPU_8\verify.py "单机全量RUN目录" --replay
python .\NPU_8\export.py "单机全量RUN目录" .\NPU_8\submission
```

## 两台电脑分开运行的启动指令

将同一份仓库复制到两台电脑，至少包含完整的 `NPU_8/`（含 `reference/`）、`code/`、`data/`。两台使用相同 Python 3.12 环境、源码和预算；计算过程中不要改算法或种子。以下在各自仓库根目录的 PowerShell 执行。当前电脑已存在 `npu312` 环境且安装了 Matplotlib。

两台电脑分别准备环境（若已有同名可用环境，直接激活）：

```powershell
conda activate npu312
python -m pip install -r .\NPU_8\requirements.txt
```

孟凡罡电脑（`--machine 1`）：问题一全部 100 例，问题三 case_051～case_100，均为 2～5 核。

```powershell
python -u .\NPU_8\run.py --machine 1 --workers 12 --small-budget 120 --large-budget 80 --timeout-minutes 180
```

陈卓钰电脑（`--machine 2`）：问题二全部 100 例，问题三 case_001～case_050，均为 2～5 核。

```powershell
python -u .\NPU_8\run.py --machine 2 --workers 12 --small-budget 120 --large-budget 80 --timeout-minutes 180
```

孟凡罡电脑另外计算后 50 例的 B 方案，作为问题三无 L2 基线。这是 200 个必要的配对辅助任务；两台可独立运行，不必相互等待或传输中间结果。B 的输入种子与搜索流程不受是否在同一进程求解 A 影响。重复的 B 结果合并时必须一致，不能偷偷用不同方案的成绩拼接成 Cache 收益。

| 电脑 | 单核基准 | A 多核任务 | B 多核任务 | L2 多核任务 |
|---|---:|---:|---:|---:|
| 孟凡罡电脑 | 100 | 400 | 200（配对辅助） | 200（后 50 例） |
| 陈卓钰电脑 | 100 | 0 | 400 | 200（前 50 例） |

单机全量、双机分片和续跑均默认使用 `--workers 12`，程序省略该参数时也使用 12 个并发进程。如内存不足，可调低并发；两台不必使用相同并发。单个任务按“case/核数”串行完成所需场景，180 分钟超时作用于该组合，不是每个场景。大型图若超时，保留已经写出的场景，可提高超时继续。

仅查看分配、不运行任何实验：

```powershell
python .\NPU_8\run.py --machine 1 --dry-run
python .\NPU_8\run.py --machine 2 --dry-run
```

## 断点续跑

每次启动打印 `RUN=完整路径`。中断后使用该路径；不必重复提供 machine、用例或预算。续跑检查场景范围、输入、配置、源码和种子哈希。允许调整并发和超时，不允许在同一结果目录变更算法或预算。

```powershell
python -u .\NPU_8\run.py --run "需要续跑的RUN目录" --workers 12 --timeout-minutes 180
```

此命令适用于单机全量，也适用于孟凡罡电脑或陈卓钰电脑的分片任务。

## 两台电脑分开运行后如何合并

1. 两台都运行到控制台显示 `STATUS=complete`。分别记录本次打印的 `RUN=...` 路径；不要选择历史验证目录。若有任务失败，先在原电脑续跑完成。
2. 在任意一台用于汇总的电脑上准备相同版本的 `NPU_8/`、`code/`、`data/` 和 Python 环境。把两台的**整个 RUN 文件夹**复制过来，包含 `manifest.json`、`baseline/`、`problem_*/jobs/`、`problem_*/plans/` 和 `reports/`；不能只复制 CSV。
3. 例如将孟凡罡电脑的 RUN 文件夹复制并命名为 `NPU_8/collected/mengfangang/`，陈卓钰电脑的复制并命名为 `NPU_8/collected/chenzhuoyu/`。两个目录内应直接有 `manifest.json`，不要多套一层原 RUN 文件夹。
4. 在汇总电脑的仓库根目录执行以下命令。若使用了不同的收集路径，替换对应目录即可：

```powershell
conda activate npu312
python .\NPU_8\merge_runs.py .\NPU_8\collected\mengfangang .\NPU_8\collected\chenzhuoyu --output .\NPU_8\runs\full_merged
python .\NPU_8\verify.py .\NPU_8\runs\full_merged --replay
python .\NPU_8\export.py .\NPU_8\runs\full_merged .\NPU_8\submission
```

按顺序执行，上一条成功后再执行下一条；核验应输出 `status: ok`。`--replay` 会重新调用官方模拟器复核代表性方案，不会重新搜索或重跑全量。合并和导出本身不执行搜索，因此不需要 `--workers` 参数。

合并输出目录 `full_merged` 必须尚不存在，导出目录 `submission` 必须为空或不存在；已有同名结果时换一个新目录名，并同步修改后续指令。合并拒绝不完整输入、不同算法/预算/配置、不同输入图，以及重复 B 方案或候选池不一致。若被拒绝，应按错误信息检查两台版本与运行参数，不要手工覆盖 job 或修改 manifest 绕过检查。

合并后，A 取孟凡罡电脑的 400 组，B 合并并去重为 400 组，L2 将前后各 200 组合为 400 组。两份单核基准和重复 B 配置须一致。程序重新生成三问逐例汇总、平均曲线和 Cache 对比图；不会平均两个分片的平均数，而是从逐例记录重新等权统计。结果位于 `NPU_8/runs/full_merged/`，官方格式方案位于 `NPU_8/submission/`。

每台 `status=complete` 只表示本机分配完成；完整合并后才应得到 `problem_1_complete=true`、`problem_2_complete=true`、`problem_3_complete=true`。所有 100 例、2～5 核齐全且图表生成成功时，`full_experiment_complete=true`。`reports/validation.json` 应为 `ok`，`reports/failures.csv` 应无数据行。

## 算法改动

1. **问题一保底与深搜**：官方重评历史最优；保留大张量聚类、独立分量分核和直线链融合；新增批量合并相邻 Task、现有分区重新分核、关键路径重排，以及按计算周期平衡的拆分后迁核。利用官方时间线优先处理晚结束的子图。预算内多起点搜索允许轻微扰动，但最终结果始终在全部有效候选中择优。
2. **问题二适配**：同时重评昨天 B 最优及今天 A 优质分区。除单个边界操作迁移外，增加整个张量相关子图的共同迁核；按真实张量的消费核集合和源核—目标核对估算搬运，避免按操作边重复计费。新增分别考虑 L1/UB 容量及最后使用位置的子图重排，优先减少驻留和换入换出；在固定粒度搜索前先精修历史起点。容量和执行依赖仍由官方检查，所有候选以 Makespan 优先、额外搬运量次优择优。
3. **问题三适配**：重评昨天 L2 最优、最终 B 方案及 B 前四名，保留严格同方案对照；独立优化 L2。缓存候选按官方 COPY_IN 的本地逻辑张量识别键，涵盖图输入及跨核中间数据的读取，不再把原始 DDR 节点 id 当作缓存键。去掉“共享边固定 0.45 折”的假设。新增首次读取、首次读取未完成时的重复 miss、曾插入后再 miss、超容量 miss 的字节诊断。FIFO 候选仅用于决定尝试顺序，真实命中和 DDR/L2 带宽竞争由官方模拟决定。
4. **搜索覆盖与合法性**：新旧邻域交替，不同改动类型交错，避免列表前面的拆分或合并耗尽预算。迁核插入满足子图依赖；A 做 Task 顺序预检，B/L2 遇到执行成环时启用更深预检。去重、尝试上限及官方调用预算共同限制搜索。小图默认最多 120 次官方调用，大图（非 COPY 操作超过 5,000）最多 80 次，每场景分别计数，失败的官方调用也计入预算。

本版的 B 继承问题一的分区思想和已知优质种子，并在 B 目标下独立优化；不直接读取孟凡罡电脑刚生成的 A 最终结果，因此两台独立计算的 B 可复现。局部优化没有全局最优保证。对已有 100 个测试图使用历史方案是显式的实验暖启动；新图可用 `solve.py` 从头搜索，不能宣称拥有历史种子的保底效果。

## 输出与验证

- `problem_1/summary.csv`、`problem_2/summary.csv`：逐例 Makespan、额外搬运量、加速比和来源。
- 新结果额外保存 `partition_added_copy_bytes` 和 `spill_added_copy_bytes`，区分切分复制与核内换入换出，避免把两种瓶颈混为一谈。
- `problem_3/summary.csv`：L2 专用最优方案，以及同一 B 方案无/有 L2 的配对成绩、命中率、Cache 加速比。
- `reports/problem_3_comparison.png`：相同 case 范围上的无 L2、同 B 方案加 L2、L2 专用优化三条曲线。
- `reports/problem_3_cache_gain.png`：逐例配对比值的均值。不能把 L2 专用方案的总收益称为纯 Cache 收益。
- `added_copy_bytes` 按官方定义统计逻辑新增搬运，不是 Cache 命中扣除后的物理 DDR 流量。

```powershell
python -m unittest discover -s .\NPU_8\tests -v
python .\NPU_8\solve.py .\data\case_019.json --cores 5 --scene B --budget 120 --output .\NPU_8\case_019_B.json
```

已有验证及其局限见 [验证记录](验证记录.md)。`validation_*` 是两个小算例的功能验证，不是全量实验成绩。旧运行的低速算例诊断见 `reference/low_speedup_diagnostics.csv`；下界忽略部分同步、内存及资源冲突，仅用于判断方向，不能据此承诺可达到的加速比。

复现全量历史只读分析（需要本机保留原始两次运行目录；不会调用模拟器）：

```powershell
python .\NPU_8\analyze_history.py
```
