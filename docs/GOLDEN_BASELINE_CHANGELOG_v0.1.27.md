# 黄金基线变更记录（schema v53→v54，合同格式 v1→v2）

日期：2026-09-05 夜间批次（任务书任务 B：Sheet 选择与角色确认）

## 比较对象

- 旧基线：v0.1.26 发版提交 `84906d5` 的 `tests/golden/cases.json`（DB schema 53）。
- 新基线：任务 B 批次的 `tests/golden/cases.json`（DB schema 54）。

## 逐字段差异

| 字段 | 旧基线 | 新候选 | 变更原因与证据 |
| --- | --- | --- | --- |
| `run_contract.schema_version` | 53 | 54 | 迁移 v54 新增 `raw_sheets.visible_state`（工作簿级可见状态，任务书 B1）；不改变任何金额、解析决策、异常判定或 Evidence 计数。 |
| Run Contract 签名 | — | 全部变化 | 任务书 B5：`sheet_scope` 每项纳入 `list_kind`（角色变更→签名变化→旧运行自动失效），合同 `format_version` 1→2。黄金案例只断言 `run_id_present`/`signature_present`/`schema_version`，不断言具体签名值，故期望值仅 schema_version 一处升级。 |

## 复算方式

```bash
uv run python scripts/golden_regression.py --json
# 预期：status=passed；demo_synthetic_v1 PASS；sanitized_real_template PENDING。
```

另需运行摘要缓存回归，确认摘要算法收敛到 `engine.sheet_digest` 单一实现后
行为不变：

```bash
uv run pytest tests/unit/test_sheet_cell_digest_cache.py -q
# 预期：3 passed。
```
