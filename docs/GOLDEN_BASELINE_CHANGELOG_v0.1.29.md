# 黄金基线变更记录（schema v54→v55，合同格式 v2→v3）

日期：2026-09-07 夜间批次（任务书任务 C：税口径模型）

## 比较对象

- 旧基线：v0.1.28 发版提交 `466bf7a` 的 `tests/golden/cases.json`（DB schema 54）。
- 新基线：任务 C 批次的 `tests/golden/cases.json`（DB schema 55）。

## 逐字段差异

| 字段 | 旧基线 | 新候选 | 变更原因与证据 |
| --- | --- | --- | --- |
| `run_contract.schema_version` | 54 | 55 | 迁移 v55：raw_sheets 增 tax_basis/tax_basis_source/tax_basis_reason/tax_basis_updated_at/tax_basis_actor 五列（口径结构化，任务书 C2）；line_items 增 tax_amount 列（独立税金列事实登记，C3）。不改变任何既有金额、解析决策或 Evidence 计数。 |
| Run Contract 签名 | — | 全部变化 | sheet_scope 纳入 tax_basis/tax_basis_source（C6-10：口径变化即旧运行失效），合同 `format_version` 2→3。黄金案例只断言 `run_id_present`/`signature_present`/`schema_version`，不断言具体签名值。 |

## 复算方式

```bash
uv run python scripts/golden_regression.py --json
# 预期：status=passed；demo_synthetic_v1 PASS；sanitized_real_template PENDING。
```

税口径行为回归：

```bash
uv run pytest tests/unit/test_tax_basis.py -q
# 预期：15 passed（识别/冲突/可比性/人工改判/合同失效/结转携带）。
```
