# v0.1.24 候选 黄金基线变更记录（schema v48）

本文件记录 `tests/golden/cases.json` 在阶段 C-1 候选中的变化。黄金回归只读比较，
不会自动把运行结果写回基线；任何基线变更都必须同时有原因、可复算证据和人工确认。

## 比较对象

- 旧基线：v0.1.23 标签提交 `736526b` 中的 `tests/golden/cases.json`
  （可用 `git show 736526b:tests/golden/cases.json` 复核）。
- 新基线：当前阶段 C-1 候选的 `tests/golden/cases.json`。

## 逐字段差异

| 字段 | 旧基线 | 新候选 | 变更原因与证据 |
| --- | --- | --- | --- |
| `run_contract.schema_version` | 47 | 48 | 合同事实确认生命周期迁移（contract_facts 增加 review_status/reviewed_at/reviewed_by/review_reason 四列）使项目结构版本升到 v48；该迁移不改变任何金额、解析决策、异常判定或 Evidence 计数。 |

## 复算方式

```bash
uv run python scripts/golden_regression.py --json
# 预期：status=passed；demo_synthetic_v1 comparison_status=PASS；
# sanitized_real_template 保持 not_available/PENDING（真实案例未登记，不冒充）。
```

除上述单字段外，基线其余全部内容与 v0.1.23 基线逐字段一致；本次变更由阶段 C-1
迁移（migrations.py v48）直接导致，属预期的结构性版本升级，非业务行为变化。
