# npu_7：自适应官方预检与拓扑约束邻域

第七版从 `npu_6` 独立复制，保留第六版的三来源种子重评、大图粒度细化、A/B 独立搜索、B 前四名候选池和 L2 配对评估。新增 `--v6-run`，可将已验证的第六版最终方案也作为种子，仍以当前官方模拟器重新评分。官方 `code/` 未改动。

算法改动：

1. 对迁移与拆分后迁移，按官方 `derive_multicore_plan` 返回的子图商图前驱、后继限定目标核插入区间；相邻交换只对无传递依赖关系的子图尝试。
2. 对 A 继续执行官方 Task 顺序 DAG 预检查。对 B/L2，起初仅做结构检查；若完整官方评估累计发现两次全局执行成环，再启用官方 `_build_scene_b_tasks` + `validate_execution` 作为候选预检。预检合法后仍必须调用完整官方评估器取分。这避免无成环配置重复构造任务的时间浪费。
3. 移除固定六轮上限。当前两个起点都停滞后，按已评估候选的排名启用未尝试过的起点；搜索在预算耗尽或候选池没有可用新起点时停止。另对去重后的候选尝试设置 `max(4×预算, 160)` 上限，防止大量静态拒绝候选导致搜索时间失控。
4. 预算仍保守地定义为**完整官方评估器调用次数**，包括其中失败的调用。预检拒绝的候选单列计数，不占这个预算。记录 `preflight_seconds`、`execution_preflight_calls`、`execution_cycle_failures`、`search_restarts` 与 `unused_budget`，便于审查实际成本。不能把“预检拒绝”误称为免费。

运行示例（在仓库根目录）：

```powershell
python .\npu_7\run.py --cases case_019 --cores 2 --workers 1 --small-budget 72 --large-budget 44 --v4-run .\多核调度_第四版\runs\20260923_141723_v4 --v6-run <第六版运行目录> --no-plots
python -m unittest discover -s .\npu_7\tests -v
python .\npu_7\verify.py <第七版运行目录> --replay
```

`--cases` 与 `--cores` 会形成笛卡尔积。针对退步清单中的 36 个场景，实际只有 23 个不同的“算例/核数”组合，应按组合运行，以免额外计算。大型算例特别是 case_014 需要单独规划资源。36 项完整回归结果、复核方法和局限见 [完整回归验证记录](完整回归验证记录_20260924.md)。
