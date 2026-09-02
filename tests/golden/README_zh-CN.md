# 价盾黄金回归库

`cases.json` 是 P0-04 的受控案例登记表。每个可用案例必须记录输入文件的相对路径、
严格 64 位十六进制 SHA-256、文件类型、方向和脱敏状态，并在 `expected` 中锁定文件数、工作表数、自动识别数、待确认数、
期次数、明细行数、各期/累计金额、匹配关系、已知异常和关键 Evidence 数量。

合成登记表的 `registry_kind` 固定为 `synthetic_demo`，可用输入只能来自
`examples/demo/`；脱敏真实登记表的 `registry_kind` 固定为
`anonymized_real_project`，可用输入只能来自
`tests/anonymized_golden_cases/assets/`，并且需要授权、脱敏和人工核验的
`provenance` 证据。执行器不会仅凭 `case_kind=sanitized_real` 计入真实案例，
也不会接受把演示路径改名后冒充真实案例。

当前仓库只有 `synthetic_demo` 匿名演示案例。它用于证明回归执行器和确定性输入链路可重复，
不能替代真实项目验收。`sanitized_real_template` 明确登记为 `not_available`，直到用户提供
经过授权、脱敏和 SHA-256 核验的真实项目副本。

执行器是只读比较器：

```bash
uv run python scripts/golden_regression.py --json
uv run python scripts/golden_regression.py --require-real
uv run python scripts/golden_regression.py --require-complete
```

任何金额、数量、匹配关系、异常集合或 Evidence 关键计数变化都会在 `diffs` 中给出字段路径、
期望值、实际值和复核原因。执行器没有“更新黄金结果”模式；变更必须由人工核对输入、规则、
运行契约和证据后，单独审阅并修改 `cases.json`。

每个案例同时输出统一的 `comparison_status`：

- `PASS`：输入可用且所有已登记黄金指标一致；
- `FAIL`：输入可用但至少一个黄金指标发生差异；
- `PENDING`：资料尚未登记或暂不可用，不能当作真实覆盖；
- `INCOMPARABLE`：执行失败或证据不足，当前没有可比的完整结果。

汇总报告另外输出 `overall_comparison_status`。只要存在 `INCOMPARABLE`、`FAIL` 或
`PENDING`，汇总状态就不会是 `PASS`；旧的顶层 `status=passed/failed` 仅为兼容旧
读取器保留，不能单独解释为真实黄金回归或生产发布通过。命令行会优先显示当前
canonical 比较状态，发布门槛也按该状态阻断。

旧的 `status` 字段暂时保留给既有发布检查读取；发布门槛仍会把真实案例数量和
PENDING/INCOMPARABLE 限制单独列出。

案例运行建立独立临时项目，原始输入只读导入，默认结束后清理可再生现场。禁止登记
`local_private_data/` 或任何绝对路径。

缺失或格式错误的 SHA-256 会使登记表在加载阶段直接失败；执行器不会以“未提供哈希”
的输入继续运行，也不会自动替换或更新黄金文件。
