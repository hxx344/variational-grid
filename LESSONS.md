# Lessons from merged work

这里保存 variational-grid 的合并经验。改动前先读本文件。每条先给加粗祈使规则，再给证据（SHA / 现象），结尾 `Seen N× (日期…)`。满 3 个不同复盘日迁入 [docs/code-standards.md](docs/code-standards.md) 并在此留 Moved 索引。

## Chart sampling

**承诺语义不变的采样优化必须用旧实现作 oracle，并断言不 mutate 输入。**

- 证据：`50ef16fc` 单遍 `reduce_points`；`tests/test_history_reduction.py` 用冻结的 `original_reduce_points` 跨 width/count/limit 对比，且短输入返回同一对象引用。
- Seen 1× (2026-10-13)

**仪表盘日期格式化在图表与表格间复用，避免每格重建 formatter。**

- 证据：`2257618a` 改 `variational_grid/web/model.js`；`tests/test_dashboard_model.cjs`。
- Seen 1× (2026-10-13)

## Theme / docs

本窗口另有 `fcf7e380`（深色主题），纯样式，无缺陷类教训。

## Moved to the code standards

尚无。
