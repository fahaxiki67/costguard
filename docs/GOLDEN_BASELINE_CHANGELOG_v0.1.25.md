# v0.1.25 候选 黄金基线变更记录（schema v50→v51）

## 比较对象

- 旧基线：阶段三提交 `8813378` 中的 `tests/golden/cases.json`（schema 50）。
- 新基线：当前费率规则候选的 `tests/golden/cases.json`（schema 51）。

## 逐字段差异

| 字段 | 旧基线 | 新候选 | 变更原因与证据 |
| --- | --- | --- | --- |
| `run_contract.schema_version` | 50 | 51 | 框架/管理性协议费率规则迁移（新增 rate_rules 表）使结构版本升到 v51；不改变任何金额、解析决策、异常判定或 Evidence 计数。 |

## 复算方式

```bash
uv run python scripts/golden_regression.py --json
# 预期：status=passed；demo_synthetic_v1 PASS；sanitized_real_template PENDING。
```

## 补充（04:5x）：schema 51→53 跨版本更新

v0.1.25 发版后连续合入 v52（list_kind）和 v53（sheet_cell_digests）两次迁移，
golden 基线的 run_contract.schema_version 从 51 一步跳到 53。变更原因同前述
（结构性版本升级，非业务行为变化），复算方式同上。
