# v0.1.25 候选 黄金基线变更记录（schema v49）

本文件记录 `tests/golden/cases.json` 在阶段 C-2 候选中的变化。黄金回归只读比较，
不会自动把运行结果写回基线；任何基线变更都必须同时有原因、可复算证据和人工确认。

## 比较对象

- 旧基线：v0.1.24 标签提交 `a8efa84` 中的 `tests/golden/cases.json`
  （可用 `git show a8efa84:tests/golden/cases.json` 复核）。
- 新基线：当前阶段 C-2 候选的 `tests/golden/cases.json`。

## 逐字段差异

| 字段 | 旧基线 | 新候选 | 变更原因与证据 |
| --- | --- | --- | --- |
| `run_contract.schema_version` | 48 | 49 | PDF 逐页人工对照复核迁移（新增 pdf_page_reviews 表）使项目结构版本升到 v49；该迁移不改变任何金额、解析决策、异常判定或 Evidence 计数。 |

## 复算方式

```bash
uv run python scripts/golden_regression.py --json
# 预期：status=passed；demo_synthetic_v1 comparison_status=PASS；
# sanitized_real_template 保持 not_available/PENDING（真实案例未登记，不冒充）。
```

除上述单字段外，基线其余全部内容与 v0.1.24 基线逐字段一致；本次变更由阶段 C-2
迁移（migrations.py v49）直接导致，属预期的结构性版本升级，非业务行为变化。
