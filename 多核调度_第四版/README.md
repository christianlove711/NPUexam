# 多核调度第四版：扩大粒度搜索

本版入口为 **improve_v4.py**。第三版仅评估两档固定粒度，本版扩大分块粒度，并对大图做粗搜及一次邻域细搜。A、B 各自按官方评估选择，L2 固定新的 B 方案配对，并另试 B 场景成绩最好的至多三个候选。

## 运行

在本目录执行：

```powershell
python -m pip install -r requirements.txt
python improve_v4.py --workers 4
```

快速检查：`python improve_v4.py --cases case_001 case_019 --cores 2 5 --workers 2`。

仓库已提供正式实验使用的第三版基准 `../多核调度_第三版/runs/20260923_123805_9a3ab0`。本目录的 `improve_v4.py` 与正式实验记录中的源码哈希一致。续跑：`python improve_v4.py --run runs/已有目录 --workers 4`。只跳过三问均已保存的用例/核数组合。续跑检查求解器源码哈希，源码已变时必须建立新运行。没有 Matplotlib 时可加 `--no-plots`，安装后续跑补图。

迁移电脑时保留仓库目录结构：上一级官方 `code/`、`data/`、第一版目录和第三版完整运行目录。第三版最优方案、指标和单核基准作为保底/参照复用，来源哈希保存在第四版记录中。完整第四版运行记录位于 `runs/20260923_141723_v4`，包括逐组方案、官方评估指标、报告及核验结果。

## 核验

```powershell
python verify_run.py runs/运行目录
python audit_v4.py runs/运行目录 --replay
python bottlenecks.py runs/运行目录
```

verify_run.py 是静态完整性与配对检查，不能称为官方重新模拟。audit_v4.py 对全部最终方案调用官方合法性检查，并以 --replay 重跑每场景/核数改进最大的方案及每场景一个未变方案。

[第四版建模说明](第四版建模说明.md) 说明搜索范围和局限。结果在运行目录 reports 下。继承的 run_all.py、worker.py 和旧文档仅作为基础组件，其默认候选池仍属于第三版，不是第四版优化入口。

本版代码与说明由 AI 辅助产生，队伍需自行核对模型、公式及比赛要求，并记录真实使用情况。
