# 多核调度第一版

本目录只放我们的算法、实验结果和说明。赛题原附件位于同级目录 `通用神经网络处理器下的多核调度问题  附件`，其中的 `code/`、`data/`、`docs/` 保持原样。交接时请把这两个目录一起交给队友，并保持同级关系。

```text
A题/
├─ 通用神经网络处理器下的多核调度问题  附件/  # 官方材料，不修改
│  ├─ code/
│  ├─ data/
│  └─ docs/
└─ 多核调度_第一版/                          # 我们的工作
   ├─ code/solve_multicore_v1.py            # 生成方案
   ├─ code/batch_v1.py                      # 批量调用官方评测函数
   ├─ results/                              # 方案、指标和汇总表
   ├─ environment.yml                      # 推荐的 Conda 环境
   ├─ requirements-plot.txt                 # 可选绘图依赖
   └─ README.md
```

## 运行

在 `多核调度_第一版` 目录下运行。`A` 对应问题 1，`B` 对应问题 2，`L2` 对应问题 3；核数为 2～5。需要 Python 3，无需 GPU 或第三方 Python 包。

推荐先安装 Miniconda，再在本目录运行 `conda env create -f environment.yml` 和 `conda activate npu-scheduling`。环境包含 Python 3.12、NumPy、pandas、Matplotlib 和 Seaborn；求解及官方评测本身只使用标准库。VS Code 的 **Python: Select Interpreter** 中选择 `npu-scheduling` 环境。未使用 Conda 的队友也可用 `requirements-plot.txt` 安装绘图依赖。

生成单个方案，默认写入本目录的 `results/`：

```powershell
python code/solve_multicore_v1.py "../通用神经网络处理器下的多核调度问题  附件/data/case_019.json" -n 4 --scene A
```

批量生成并评测三组代表性用例，默认也写入本目录的 `results/`：

```powershell
python code/batch_v1.py "../通用神经网络处理器下的多核调度问题  附件/data/case_019.json" "../通用神经网络处理器下的多核调度问题  附件/data/case_035.json" "../通用神经网络处理器下的多核调度问题  附件/data/case_014.json" -n 2 3 4 5 --scenes A B L2
```

评测调用的是官方 `evaluate_scene_a`、`evaluate_scene_b` 和 `evaluate_problem_3` 函数。每个方案保存为独立 JSON，每组简要指标保存为 `_metrics.json`，汇总表为 [`results/summary.csv`](results/summary.csv)。没有生成占空间较大的 Trace；如需 Trace，可用官方命令和本目录中的方案单独评测，并通过 `-o`、`--trace-output`、`--log-output` 把所有输出指定到本目录 `results/`。

如需改变官方代码位置，可将环境变量 `MULTICORE_OFFICIAL_CODE` 设置为官方 `code` 目录的绝对路径。

## 算法和当前结果

算法先把 Tensor 和原有 COPY 节点连接的依赖收缩为计算操作 DAG，再按关键路径优先生成确定性拓扑序；沿拓扑序结合操作周期和边界数据量划分连续子图，最后用预计完成时间、通信量和场景同步延迟贪心分核。L2 的通信代价折扣仅用于估计，成绩以官方仿真为准。

已对 `case_019`、`case_035`、`case_014` 的 2～5 核、三种场景共 36 组配置完成评测，全部成功。详细 Makespan、额外搬运量、Cache 命中率及耗时见 `results/summary.csv`。这只是第一版代表性测试，尚未完成 100 组全量实验，也未计算正式平均加速比。

当前 B 与 L2 分别生成方案。若要隔离 L2 Cache 本身的效果，应把**同一份方案**分别送入问题 2 和问题 3 的官方评测器。
