# Jiadun（价盾）v0.1.31 — 控制基准结论入审核问题中心 + OOXML 解析加固预发行

## 本版定位

v0.1.31 是在 v0.1.30 预览候选基础上的功能预发行，落地 ROADMAP v0.1.25 遗留项（任务书
任务 E3）：对上控制基准的五态上限比较结论进入统一审核问题生命周期，并随导出与报告
自动携带；同时以 defusedxml 加固 OOXML ZIP 部件解析。本版仍是预览候选，不代表正式
生产能力。

## 主要变化

- 控制基准比较结论可通过 `record_comparison_finding` 登记为
  `rule_id=control_baseline_cap` 的审核问题：自动出现在审核问题中心、汇总计数、
  Excel 异常清单、新增「控制基准比较」导出页与 Word 报告 Top 风险事项，并绑定
  当前 Run Contract 签名。
- 同一基准再次比较时旧结论连同证据按快照语义转历史（不删除、不篡改），不同基准
  结论互不覆盖；重跑异常检测不清扫该规则（输入未变时结论仍然成立）。
- FAIL 结论保持只报告超出金额，不构成违规、责任或最终审定结论；工作台
  「对上控制基准…」对话框比较后自动入册，入册失败时明确提示且不伪装成功
  （附 fail-closed 回归测试）。
- OOXML ZIP 部件解析统一走 defusedxml 入口：源工作簿是不可信输入，标准库
  ElementTree 会展开内部 DTD 实体（billion laughs）造成资源耗尽，现一律拒绝
  DTD/内部实体（fail-closed）。
- 文档第二遍审阅：修正 QUICKSTART 中工作台标签页与 OCR 能力的过期描述、清理
  labels 死常量。

## 验证结果

- 全量 pytest（含 180 行控制基线新测试与入册失败 fail-closed 回归）、Ruff 通过；
  macOS arm64 与 Windows x64 CI 在推送后覆盖本版代码。
- 本 Release 由维护者在 `main` 上打 tag，并以独立平台构建产物和 SHA-256 清单发布。

## 仍未关闭的生产门槛

- 真实脱敏黄金案例与真实扫描 PDF OCR 质量回归。
- macOS Excel、Windows Excel、macOS WPS、Windows WPS 真机验收。
- 1万、5万、20万行现场性能与异常恢复验证。
- Windows 代码签名与 macOS 公证。

以上门槛未闭合前，本版保持 Preview/Prerelease；不能解释为 production ready。
