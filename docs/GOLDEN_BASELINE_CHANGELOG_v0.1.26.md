# v0.1.26 候选 黄金基线变更记录（schema v50）

本文件记录 `tests/golden/cases.json` 在对上控制基准候选（阶段三）中的变化。
黄金回归只读比较，不会自动把运行结果写回基线；任何基线变更都必须同时有原因、
可复算证据和人工确认。

## 比较对象

- 旧基线：C-2 提交 `4b4536f` 中的 `tests/golden/cases.json`
  （可用 `git show 4b4536f:tests/golden/cases.json` 复核）。
- 新基线：当前对上控制基准候选的 `tests/golden/cases.json`。

## 逐字段差异

| 字段 | 旧基线 | 新候选 | 变更原因与证据 |
| --- | --- | --- | --- |
| `run_contract.schema_version` | 49 | 50 | 对上控制基准迁移（新增 control_baselines 表）使项目结构版本升到 v50；该迁移不改变任何金额、解析决策、异常判定或 Evidence 计数。 |

## 复算方式

```bash
uv run python scripts/golden_regression.py --json
# 预期：status=passed；demo_synthetic_v1 comparison_status=PASS；
# sanitized_real_template 保持 not_available/PENDING（真实案例未登记，不冒充）。
```

除上述单字段外，基线其余全部内容与上一基线逐字段一致；本次变更由
migrations.py v50 直接导致，属预期的结构性版本升级，非业务行为变化。
