# NPU_10：根据 NPU_9 全量结果继续优化

保留 NPU_9 及其运行结果，官方 `code/`、配置与输入均未修改。

继承的运行：`NPU_9/runs/20260925_100256_747201`。该运行是 **100 case、五核、三个问题**，均值分别为 **3.955670755 / 4.453267061 / 4.566842224**，不是 2—5 核全部运行。

新版已打包这次结果、候选缓存及本次有限验证的改善。运行不依赖旧版本目录，只需要同级的 `code`、`data` 和完整 `NPU_10`（必须包括 `seedpack`）。

## 三个问题分别运行全量

在 `C:\Users\chris\Desktop\NPUexam-1`、`npu312` 环境执行。以下每题包含 **100 case × 2/3/4/5 核**，先跑五核。三个输出目录本次尚未创建。

命令中的目录名必须写成 `NPU_10`、`problem1_full`，下划线前不要再加反斜杠。程序现在会兼容误复制的 `NPU\_10` 形式，但 PowerShell 仍建议使用下面原样命令。

**按顺序运行，上一题显示 `STATUS=complete` 后再启动下一题。** 每条可以在独立 PowerShell 窗口执行，不依赖前一个窗口的变量。

问题一：

```powershell
python -u .\NPU_10\run.py --scenes A --cores 5 2 3 4 --workers 12 --run .\NPU_10\runs\problem1_full --plots
```

问题二，继承问题一这次的新结果：

```powershell
python -u .\NPU_10\run.py --scenes B --cores 5 2 3 4 --workers 12 --upstream-run .\NPU_10\runs\problem1_full --run .\NPU_10\runs\problem2_full --plots
```

问题三，继承问题一、二的新结果，固定问题二的配对方案：

```powershell
python -u .\NPU_10\run.py --scenes L2 --cores 5 2 3 4 --workers 12 --upstream-run .\NPU_10\runs\problem1_full .\NPU_10\runs\problem2_full --run .\NPU_10\runs\problem3_full --plots
```

若此次仍只想测五核，把**三条中的** `--cores 5 2 3 4` 都改成 `--cores 5`。上下游 case/核数范围必须对应，不能用只含五核的上游作为四种核数全部实验的完整输入。

问题三目录内会保留 `problem_2` 作为配对证据：它是继承的 B 结果，**B 新搜索调用数为 0**。问题三只做当前 B 方案开启 L2 的必要评估，并搜索 L2 方案。若必要配对无法完成，会标记不完整，而不是用不同方案的分母凑出结果。

不指定 `--upstream-run` 也能分别运行，起点是随新版打包的结果；上述顺序能让后题继承前题刚得到的改进。

## 续跑、再精修与合并

中断问题二后续跑，不要再加 `--upstream-run`：

```powershell
python -u .\NPU_10\run.py --run .\NPU_10\runs\problem2_full --workers 12 --plots
```

完成目录默认只复用结果。要额外精修，例如问题一指定 case 的五核：

```powershell
python -u .\NPU_10\run.py --run .\NPU_10\runs\problem1_full --refine --scenes A --cases 9,40,48,68 --cores 5 --workers 12
```

源码或种子包变动后不能继续旧 RUN，应创建新目录。开发验证目录用于保留证据，不作为正式运行入口。

三个问题完成后统一合并和导出：

```powershell
python .\NPU_10\merge_runs.py .\NPU_10\runs\problem1_full .\NPU_10\runs\problem2_full .\NPU_10\runs\problem3_full --output .\NPU_10\runs\all_merged
python .\NPU_10\verify.py .\NPU_10\runs\all_merged
python .\NPU_10\export.py .\NPU_10\runs\all_merged .\NPU_10\submission
```

合并和导出目标目录必须不存在。合并保留同来源的 B/L2 配对，检查基线、输入、配置、官方代码和结果一致性。

## 本轮调整

- 根据当前最好方案做迁移、拆分、融合和排序，优先补充上一版已耗尽的候选集合。
- 深度划分增加附近宽度和错位边界；兄弟分支尝试不同分组数，仍使用命令指定的实际 NPU 核数。
- 非法局部划分只跳过该候选，不再因遗漏 `MulticoreCutError` 而截断后续搜索。
- A 检查任务顺序环；B/L2 对局部候选增加执行依赖检查，减少无效模拟。
- 继承已知超时记录；跳过较大图的过细 `depth_islands_2` 候选。
- 缓存命中不重复压缩整个缓存；只有新评分或更优解才触发相应缓存保存。
- 新增 `--upstream-run`，支持 A → B → L2 独立运行，并固定 L2 的 B 配对来源。

默认仍是 workers=12，小/大 case 每场景新评估 12/6 次、搜索预算 60/150 秒、单次评估硬超时 15/60 秒，巨大图并发上限 2。没有改回 120/80 次搜索。已有强结果默认保留；确实需要继续优化时加 `--polish-strong`。

当前最新有限验证结果已纳入种子包，五核组合均值为 **3.957247876 / 4.460261118 / 4.573890631**。这不是新算法又跑完一轮全量后的成绩，不能据此声称 A 已超过 4.0 或 B 已超过 4.5。测试与具体改善见 `优化与验证说明.md`。
