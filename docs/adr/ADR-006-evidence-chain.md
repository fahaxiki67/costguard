# ADR-006: 证据链与审计日志
状态: Accepted | 日期: 2026-08-29
## 决策
- 每个差异/异常/风险/结论持有 evidence_id（表 evidence）。
- 证据内容：summary + steps_json(计算过程) + sources_json(文件+Sheet+单元格+原始内容)。
- 人工修改一律写 audit_log（before/after JSON + reason），并生成对应证据。
## 理由
原则 4/5/12/14：所有计算可追溯、结论可回溯至原始位置、结果同时保留过程与来源、
人工修正必须留痕。
