# 脱敏真实项目黄金回归

此目录是 Jiadun（价盾）的真实项目回归入口。资料必须先脱敏，再登记到
`cases.json`；原始工程文件、客户名称、合同编号、个人信息和本机私有路径
不得进入仓库。

登记表的 `registry_kind` 必须为 `anonymized_real_project`。可执行真实案例的
输入只能放在本目录下的 `assets/`（登记路径形如
`tests/anonymized_golden_cases/assets/<脱敏文件>`），不得引用
`examples/demo/` 演示资产。每个案例还必须有唯一 `case_version`，并在
`provenance` 中记录 `authorized=true`、`anonymized=true`、核验人、核验时间和
核验说明；仅把 `case_kind` 改成 `sanitized_real` 不能获得真实覆盖资格。

每个 `available` 案例至少应固化：

- 控制总金额、有效明细行数和关键分项金额；
- 各期累计、对上/对下金额；
- 已知异常、匹配结果和不可比项目；
- 关键 Evidence 数量及其文件、Sheet、行列定位。
- 每个输入文件的相对路径、类型、方向和严格 64 位十六进制 SHA-256；

可用案例缺少或填写错误的 SHA-256 时，黄金执行器会在登记表加载阶段拒绝该案例，
不得把输入文件身份留给运行时“如果存在才校验”的可选逻辑。

黄金结果是只读发布基线。每次运行只比较实际结果，不自动更新本文件；金额、
行数、匹配关系、异常或 Evidence 发生变化时必须输出差异并停止发布门槛。

当前仅保留一个“待提供资料”占位登记，不能视为已经完成真实项目覆盖。合成
演示案例仍在 `tests/golden/cases.json`，只用于验证回归执行器本身。占位案例的
`comparison_status` 为 `PENDING`；资料缺失时不得用 0 或合成数据填充。
黄金回归汇总的 `overall_comparison_status` 也会保持 `PENDING`；旧的
`status=passed` 仅为兼容字段，不能据此形成真实项目结论或生产发布结论。
