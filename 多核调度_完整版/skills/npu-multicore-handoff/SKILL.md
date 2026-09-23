---
name: npu-multicore-handoff
description: Continue, validate, and explain this project's multicore NPU scheduling solver and 100-case experiments. Use when taking over implementation, reproducing results, interpreting A/B/L2 comparisons, or preparing the modeling report for this specific contest folder.
---

# Multicore NPU project handoff

Find the project by locating `多核调度_完整版/run_all.py` in the contest attachment folder. Read its `README.md` and `交接记录.md` first. For algorithm work read `算法与实验方案.md` and `代码结构与函数说明.md`; for execution read `运行与环境说明.md`; for figures, metrics, or report writing read `结果字段与图表解读.md`. The official evaluator is the sibling `code/` and its fixed configuration is `data/config.txt`; preserve both. The original `多核调度_第一版/` is also an import dependency.

The user's current workflow is to write algorithms on the Mac and run evaluations/search on a stronger CPU computer. Documentation changes do not authorize restarting Mac batch experiments. Prepare commands and transferable files accordingly; follow later explicit user changes to this preference.

On the target run computer, use `python 多核调度_完整版/run_all.py --cases case_019 --cores 2 --search-hours 0` for a small check, then `--quick`, then the default command for 100 cases. A new run gets a unique timestamp directory. Continue an interrupted run only with `--resume`; use `--retry-failed` after diagnosing errors. Resume checks configuration hashes but does not fingerprint solver source or graph contents, so changed algorithms need new formal runs. Inspect the actual cases/cores in `manifest.json`, not just its completion status.

For problem 3, compute the isolated L2 effect only from `paired_no_l2_makespan` and `paired_with_l2_makespan`, which use the same problem-2 plan. The optimized problem-3 `makespan` can use a different plan. For problems 1 and 2, use the official single-core result and the arithmetic mean of per-case speedups.

The six PNGs are summaries of the selected cases. Connecting lines do not represent untested core counts. The official `added_copy_bytes` counter is constructed before Cache hit simulation and is not the post-hit physical DDR traffic. Current local search checks one bounded neighborhood per configuration, and the recorded seed does not drive randomized search. Explain these actual behaviors rather than claiming a full FIFO optimizer, multi-round search, or full-dataset completion.

When continuing work, preserve previous run directories, explain which source files changed, run the tests and representative cases, and append results plus remaining work to `交接记录.md`. Follow the user's current instructions if they change the workflow or priorities.
