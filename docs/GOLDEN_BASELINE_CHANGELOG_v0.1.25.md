# v0.1.25 候选（Unreleased） 黄金基线变更记录（schema v50）

本文件记录 `tests/golden/cases.json` 在阶段 C-2（对上控制基准候选）候选中的变化。
黄金回归只读比较，不会自动把运行结果写回基线；任何基线变更都必须同时有
原因、可复算证据和人工确认。

## 比较对象

- 旧基线：v0.1.24 标签提交 `a8efa84` 中的 `tests/golden/cases.json`
  （可用 `git show a8efa84:tests/golden/cases.json` 复核）。
- 新基线：阶段 C-2 候选分支 `feat/control-baseline-candidates` 的
  `tests/golden/cases.json`。

## 逐字段差异

| 字段 | 旧基线 | 新候选 | 变更原因与证据 |
| --- | --- | --- | --- |
| `run_contract.schema_version` | 48 | 50 | 对上控制基准候选迁移（新表 `control_baselines` + 项目归属守卫触发器 + supersedes 唯一索引）使项目结构版本升到 v50；v49 为并行开发中的 PDF 逐页复核阶段预留，本分支不占用。该迁移不改变任何金额、解析决策、异常判定或 Evidence 计数；演示项目没有登记控制基准，运行契约 `control_baselines` 载荷为空列表。 |

## 复算方式

```bash
uv run python scripts/golden_regression.py --json
# 预期：status=passed；demo_synthetic_v1 comparison_status=PASS；
# sanitized_real_template 保持 not_available/PENDING（真实案例未登记，不冒充）。
```

除上述单字段外，基线其余全部内容与 v0.1.24 基线逐字段一致；本次变更由阶段
C-2 迁移（migrations.py v50）直接导致，属预期的结构性版本升级，非业务行为变化。
