# 价盾黄金回归库

`cases.json` 是 P0-04 的受控案例登记表。每个可用案例必须记录输入文件的相对路径、
SHA-256、方向和脱敏状态，并在 `expected` 中锁定文件数、工作表数、自动识别数、待确认数、
期次数、明细行数、各期/累计金额、匹配关系、已知异常和关键 Evidence 数量。

当前仓库只有 `synthetic_demo` 匿名演示案例。它用于证明回归执行器和确定性输入链路可重复，
不能替代真实项目验收。`sanitized_real_template` 明确登记为 `not_available`，直到用户提供
经过授权、脱敏和 SHA-256 核验的真实项目副本。

执行器是只读比较器：

```bash
uv run python scripts/golden_regression.py --json
uv run python scripts/golden_regression.py --require-real
```

任何金额、数量、匹配关系、异常集合或 Evidence 关键计数变化都会在 `diffs` 中给出字段路径、
期望值、实际值和复核原因。执行器没有“更新黄金结果”模式；变更必须由人工核对输入、规则、
运行契约和证据后，单独审阅并修改 `cases.json`。

案例运行建立独立临时项目，原始输入只读导入，默认结束后清理可再生现场。禁止登记
`local_private_data/` 或任何绝对路径。
