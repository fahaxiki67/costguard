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

## 补充（2026-09-05 接力二轮）：schema 53→54 更新（结论性框架）

迁移 v54（control_conclusions + rate_rule_applications 两张只追加表）使
run_contract.schema_version 升到 54；本轮同步只更新该字段——结构性版本
升级，不改变金额、解析决策、异常判定。

**遗留差异（未更新基线，禁止静默放行）**：同工作树存在并行会话未提交的
`extract.py` 改动（party 类候选加 guard、中文大写金额支持），黄金复算
`evidence.by_kind.contract_fact` 13→8、`evidence.current_count` 126→121。
该差异属并行任务的在制行为变化，不属于本轮变更；待其收口后由该任务
自行按流程核对并更新基线。当前 `golden_regression --json` 预期
`status=failed`（mismatch 仅限上述两字段），不得视为通过。
