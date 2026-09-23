# 多核调度第三版

本目录在队友交付的完整版上新增编号优先的拓扑切分候选。改动、数学含义、Windows 快速对照和全量验证边界见 [第三版改进说明](第三版改进说明.md)。其余模块的使用方式沿用下文。运行结果保存在本目录独立的 `runs/` 中。

本目录包含第一版回退、第二版求解器与第三版新增候选、题目一至三的批量评估、可复现结果和交接材料。赛题原有 `code/`、`data/`、`docs/` 和 `多核调度_第一版/` 位于本目录的上一级；运行时不要改动官方评估代码或 `data/config.txt`。把整个赛题附件目录复制到另一台电脑即可保留这些相对路径。

本目录已在 Windows 上验证，Mac 与 Windows 均可运行；全量实验请使用同一运行目录续跑并保存清单。

## 文档导航

| 你想了解什么 | 阅读文档 |
|---|---|
| 如何安装、迁移、启动、续跑，以及所有命令行参数 | [运行与环境说明](运行与环境说明.md) |
| 问题如何建模，候选如何构造，近似评分如何计算 | [算法与实验方案](算法与实验方案.md) |
| 每个 Python 文件和主要函数负责什么，修改应从哪里入手 | [代码结构与函数说明](代码结构与函数说明.md) |
| 六张图各自含义，如何读坐标和图例，CSV/JSON 每列如何解释 | [结果字段与图表解读](结果字段与图表解读.md) |
| 已完成什么、哪些结果只是快速验证、下一位队友如何接手 | [交接记录](交接记录.md) |
| 给后续 Codex 使用的项目规则 | [交接 skill](skills/npu-multicore-handoff/SKILL.md) |

第一次接手建议先读运行说明；解释截图直接打开图表解读；修改算法时结合算法说明和代码说明阅读。

## 环境

- macOS 或 Windows，64 位 Python 3.11～3.13；求解器与官方评估只用 Python 标准库。
- 绘图需要 Matplotlib。建议至少 8 GB 内存；全量评估及搜索建议 16 GB 以上和多核 CPU。磁盘预留数 GB，具体用量取决于候选与可选 Trace。
- 图表使用无界面绘图后端，不需要 MATLAB 或图形桌面。固定硬件参数从上一级 `data/config.txt` 读取。

在 `多核调度_第三版` 目录执行一次环境安装：

```bash
python -m venv .venv
```

macOS 激活：`source .venv/bin/activate`；Windows PowerShell 激活：`.venv\Scripts\Activate.ps1`。然后运行：

```bash
python -m pip install -r requirements.txt
```

若只生成 CSV 而暂不画图，可跳过 Matplotlib 并使用 `--no-plots`。

## 一键入口

从赛题附件根目录执行：

```bash
python 多核调度_第三版/run_all.py
```

该命令默认处理全部 100 个用例、2～5 核和三问，并计算单核基准；在完成必需评估后，给追加搜索 10 小时的提交预算。当前是每组配置一次有限邻域搜索，处理完可提前结束；已启动的任务和题三配对重算可能在预算到期后继续完成。默认并行进程数为 `min(4, max(1, CPU逻辑核数//2))`，可按内存和 CPU 调整。一个用例失败不会终止其余用例，结束时缺失必需结果会返回非零退出码。

先做快速验证：

```bash
python 多核调度_第三版/run_all.py --quick
```

快速模式运行 `case_001`、`case_019`、`case_035` 的 2/4 核，不进入追加搜索。最大规模压力验证应单独选择 `case_014`；更小的单例验证可用：

```bash
python 多核调度_第三版/run_all.py --cases case_019 --cores 2 --search-hours 0
```

继续中断的运行时只指定已有目录；用例和核数从其 `manifest.json` 读取：

```bash
python 多核调度_第三版/run_all.py --resume "多核调度_第三版/runs/已有运行目录"
```

失败任务默认保留诊断，不自动重试；确认环境或代码问题已经解决后加 `--retry-failed`。`--workers 2`、`--search-hours 4`、`--timeout-minutes 60` 可分别调整并行度、追加搜索时间和单任务超时。每个新命令都会建立独立的时间戳目录；只有显式 `--resume` 会继续写入旧目录。

## 结果位置和含义

每次新运行写入 `runs/年月日_时分秒_随机后缀/`：

```text
runs/<运行编号>/
├── manifest.json
├── baseline/singlecore.csv
├── problem_1/summary.csv
├── problem_1/plans/                # 题一最终方案
├── problem_2/summary.csv
├── problem_2/plans/                # 题二最终方案
├── problem_3/summary.csv
├── problem_3/plans/                # 题三优化后方案
└── reports/
    ├── aggregate.csv
    ├── failures.csv
    └── *.png                       # 曲线和第一版对照图
```

各问题的 `jobs/` 保存每组配置的候选指标和错误；追加搜索的记录在 `search_jobs/`。默认不保存体积较大的完整操作 Trace。`manifest.json` 为 `complete` 且 `failures.csv` 只有表头，表示清单所选范围的必需评估和本次要求的报告检查完成。正式全量还须确认清单有 100 个用例、核数覆盖 2～5、三问各有 400 条汇总，并通过 `validation.json` 中的检查。

`summary.csv` 中 `makespan` 是该问题最终方案的官方结果；`v1_makespan` 是第一版基线。`speedup` 是同用例单核 Makespan 除以当前 Makespan。题三的 `paired_no_l2_makespan` 与 `paired_with_l2_makespan` **使用题二选出的同一份方案**；`paired_l2_speedup` 才能解释为 Cache 本身的收益。题三 `makespan` 可来自另行优化的方案，不能与配对列混为一谈。`cache_hit_rate` 为按字节计算的命中率。

图表和汇总表始终从已保存的逐组 JSON/CSV 重建。若任务中断，重启时自动跳过已成功的任务。若 Matplotlib 缺失，数值表仍会保存，但本次运行标记为不完整；安装依赖后对同一目录使用 `--resume` 可补画图。

如需查看某个方案的完整操作 Trace，从赛题附件根目录单独调用官方命令，并把三个输出都指向该运行目录。例如题一：

```bash
python code/multicore_cut_evaluate_problem_1.py data/case_019.json "多核调度_第三版/runs/<运行编号>/problem_1/plans/case_019_2core.json" --config data/config.txt -o "多核调度_第三版/runs/<运行编号>/problem_1/case_019_2core_full.json" --trace-output "多核调度_第三版/runs/<运行编号>/problem_1/case_019_2core_trace.json" --log-output "多核调度_第三版/runs/<运行编号>/problem_1/case_019_2core_log.txt"
```

题二、题三分别换用官方 `multicore_cut_evaluate_problem_2.py`、`multicore_cut_evaluate_problem_3.py` 和对应问题目录中的方案。

## 文档与队友交接

算法、数学模型和实验口径见 [算法与实验方案.md](算法与实验方案.md)。项目内的 `skills/npu-multicore-handoff/SKILL.md` 是可复制的 Codex skill；队友将整个 `npu-multicore-handoff` 文件夹复制到各自的 `~/.codex/skills/`，或设置了 `CODEX_HOME` 时复制到 `$CODEX_HOME/skills/`，重启 Codex 后即可使用。交接状态见 [交接记录.md](交接记录.md)。

## 验证

```bash
python -m unittest discover -s tests -v
python verify_run.py runs/<运行编号>
```

测试包括方案合法性、第一版回退、第三问同方案配对、运行目录隔离与续跑。正式论文结果应使用 `run_all.py` 的全量输出，不能把快速模式或部分结果称为全部 100 例实验。
