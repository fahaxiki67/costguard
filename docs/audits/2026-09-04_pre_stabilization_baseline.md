# 稳定化阶段基线审计报告（2026-09-04）

依据《价盾 CostGuard 下一阶段完整开发任务书》§二，在进入任务 A~K 之前对当前
HEAD 建立可复核基线。本报告只记录事实；测试计数等未能精确取得的项目如实标注。

## 环境

| 项 | 值 |
| --- | --- |
| Git commit（审计时 HEAD） | bd61fda（docs: 收录用户下一阶段开发任务书） |
| 分支 | develop/v0.1.20-import-intake（= main，均已推送远端） |
| OS | Windows 10 Pro 19045 x64（本机开发/打包机，非 CI） |
| Python | 3.12.14（项目 .venv，uv 管理） |
| package version | 0.1.25（pyproject.toml，uv.lock 已同步） |
| schema version | 51（LATEST_SCHEMA_VERSION） |
| 远端 CI | 2499b60（main）completed success（Windows + macOS 双作业） |

## 门槛检查结果

| 门禁 | 结果 | 说明 |
| --- | --- | --- |
| 全量 pytest | PASS（退出码 0） | 失败 0、错误 0；跳过项均为既有环境条件跳过；用例总数约 870（精确计数以 CI 日志为准，本机日志统计行被 warnings 块截断） |
| ruff（src+scripts+tests） | PASS | All checks passed |
| Golden regression | PASS / PENDING | 合成案例 demo_synthetic_v1=PASS；脱敏真实案例 not_available→PENDING（真实样本登记为 0，不冒充） |
| release consistency（--allow-no-real） | PASS（退出码 0） | 开发模式 conditional，非生产放行 |
| performance benchmark | 未验证 | 本轮未执行（任务 F 将按 10k/50k/200k 建 3 层性能语料后正式跑） |
| Office/WPS 真机 | 未验证 | 见任务 H 四格矩阵，现全部 PENDING/NOT AVAILABLE |
| 真实扫描 PDF OCR | 未验证 | 逐页管线已有；真实语料验收待任务 G（已有真实框架协议 docx 抽取验证先例） |

## 当前能力定位（按任务书 §二 分类法）

- **已完成且已验证（自动化层面）**：Decimal 金额链、原文件只读+哈希核验、Evidence
  全链、Run Contract（含确认状态与拒绝剔除）、合同事实确认生命周期（v48）、
  PDF 逐页提取与 fail-closed（含全角％/公式型候选的费率扫描，v51）、逐页人工
  对照复核（v49）、对上控制基准候选与五态比较（v50）、费率规则候选→确认→试算、
  SheetConfirm 门控与人工确认、备份恢复安全、Windows/macOS 打包链、发布一致性
  门槛、CI（双平台 pytest+安全扫描）。
- **代码存在但缺真实验收**：真实扫描 PDF 的 OCR 质量、页级复核实机闭环、控制
  基准/费率规则在真实项目上的完整闭环、Excel 导出的 50k/200k 真实规模。
- **部分完成**：费率基数与期次合计自动关联（apply 已有、接线未做）；控制基准
  结论进 Finding/报告（未做）；结构化 scope（当前文本比对）。
- **尚未实现**：税口径结构化模型（反馈#4，任务 C）、措施费"数量视同 1"人工
  语义规则（反馈#5，任务 D）、Sheet 全列表+下拉角色+推荐理由（反馈#2，任务 B）、
  真实资料三层测试体系（任务 A，本轮已起步：盘点脚本 + 民权 3256 文件清单 +
  Golden 候选核对表已产出待用户挑选）。

## 已知失败

无（本轮全量 pytest 退出码 0，无 FAILED/ERROR）。

## 与用户反馈的对应

桌面 问题反馈.docx 首批 5 条已分诊：#1 PDF 结算导入已修（d8e1032，fail-closed
转待人工处理）、#3 预览可用性已修（2499b60）、#2/#5 对应任务 B/D、#4 对应任务 C，
排入后续批次。

## 未验证项目汇总

Golden Case 人工真值登记（0/≥15）、真实 OCR 质量、四环境 Office 真机、50k/200k
完整导出、Windows 签名与 macOS 公证——均为任务书 Release Gate 项，在闭环前保持
prerelease 定位。
