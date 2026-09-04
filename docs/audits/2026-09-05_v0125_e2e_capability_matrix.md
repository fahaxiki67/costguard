# JiaDun v0.1.25 端到端验收 · §三 现状审计与能力矩阵（2026-09-05）

依据《JiaDun v0.1.25 真实业务资料发现与端到端拖放验收任务书》§三。
执行环境说明：本轮在 **Windows 10 x64 侧**执行（任务书 §四 描述的 Mac 侧为另一执行方；
Mac 专属步骤如 Finder 拖放在 Windows 侧标记为 Mac 侧任务，Windows 侧以 Explorer
拖放做等效真实 GUI 验证）。

## 一、基线事实

| 项 | 值 |
| --- | --- |
| 基线版本 | v0.1.25（tag，GUI 试用部署 = 桌面\价盾示用版\Jiadun\Jiadun.exe） |
| 仓库 HEAD（审计时） | 877f54c（v0.1.25 + 3 项用户反馈修复 + Sheet 清单类型数据层，无新业务功能） |
| git status | clean（已全部提交推送） |
| Python | 3.12.14（.venv） |
| GUI 技术栈 | PySide6（QStackedWidget：项目列表页 ↔ 工作台页） |
| schema version | 52（含今日 list_kind 标注列） |
| package version | 0.1.25 |
| CI | main 双平台 pytest + 安全扫描（2499b60/877f54c 均绿） |
| 打包 | scripts/build_windows_x64.ps1（setup.exe+便携 zip）、package-macos-arm64.yml（DMG） |

## 二、导入与拖放链路（实测代码路径）

- **拖放**：`main_window.py` `setAcceptDrops(True)` + `dragEnterEvent/dropEvent`
  → `FileDropZone.paths_from_mime` → `_handle_source_paths(paths)`；
  `file_selection.py` 另有 FileDropZone 组件（项目创建流程）。
- **文件对话框**：工作台 `选择结算文件…`（QFileDialog 多选）、`导入资料文件夹…`、
  `导入合同/纪要…`（docx/pdf/txt 过滤）。
- **解析链**：excel_parser.parse_file（xlsx/xls/csv）→ header_detect → cleaning →
  settlement_io 物化（raw_sheets/raw_cells/line_items/period_totals）；
  PDF 合同/审计报告走 pdf_pipeline（逐页+RapidOCR，v0.1.24 起）；
  **PDF/Word 结算清单不支持（v0.1.25 起 fail-closed 转待人工处理+指引）**。

## 三、能力矩阵

| 功能 | 代码存在 | 自动测试 | 真实文件验证 | GUI 拖放验证 | 当前状态 |
| --- | --- | --- | --- | --- | --- |
| 项目创建/打开 | ✓ | ✓ | 部分（本机开发库） | 待验 | 自动测试确认 |
| 文件拖放入库 | ✓ | 部分（UI 导入流） | 待验 | **待验（本轮核心）** | 仅代码存在+自动测试 |
| xlsx/xls/csv 结算解析 | ✓ | ✓ | 部分（合成+少量真实） | 待验 | 自动测试确认 |
| PDF 合同逐页提取+OCR | ✓ | ✓（合成+离线模型） | 部分（民权审核报告复现） | 待验 | 自动测试确认 |
| PDF/Word 结算清单转待处理 | ✓ | ✓ | ✓（民权结算表 PDF 复现） | 待验 | 真实文件确认 |
| Sheet 门控/人工确认 | ✓ | ✓ | 待验 | 待验 | 自动测试确认 |
| 表头歧义人工映射 | ✓ | ✓ | 待验 | 待验 | 自动测试确认 |
| 清洗/标准化 | ✓ | ✓ | 待验 | 待验 | 自动测试确认 |
| 多期合并/累计 | ✓ | ✓ | 待验（龙泉 14 文件曾验 v0.1.18） | 待验 | 自动测试确认 |
| 对上/对下汇总与匹配 | ✓ | ✓ | 部分（龙泉黄金案例 v0.1.18） | 待验 | 自动测试确认 |
| 异常检测/Finding | ✓ | ✓ | 部分（合成） | 待验 | 自动测试确认 |
| 控制基准候选/五态比较 | ✓ | ✓（单测） | 未验证 | 未实现 GUI 拖放项 | 代码存在+自动测试 |
| 费率规则候选/试算 | ✓ | ✓（单测） | 部分（民权框架协议扫描验证） | 未验证 | 真实文件确认（抽取层） |
| 报告/Excel 导出 | ✓ | ✓ | 部分（合成 Golden） | 待验 | 自动测试确认 |
| 备份/恢复/破坏性 | ✓ | ✓ | 部分（v0.1.18 破坏性 7 场景） | — | 自动测试确认 |
| 性能基准 | ✓（脚本） | ✓（10k 合成） | 50k/200k 未完成 | — | 部分 |
| 签名/公证 | ✗ | — | — | — | 未实现（发布门槛 PENDING） |

## 四、状态口径说明

- "自动测试确认"= 仓库自动化测试对合成/脱敏样本通过；
- "真实文件确认"= 本日已用用户真实文件（隔离副本+哈希核验）验证的项；
- "待验"= 本轮端到端验收要覆盖的目标；
- Mac Finder 拖放/SMB 拖放 = Mac 侧执行方任务，本轮 Windows 侧只验证
  共享目录可访问性（供 Mac 拖放的资料源），Mac 项在 Windows 侧报告中
  标记 REAL_DATA_BLOCKED（Mac 侧）。

## 五、JiaDun 名称核对（任务书 §二十七 预登记）

- 品牌定义：PRODUCT_DISPLAY_NAME="价盾"、PRODUCT_NAME="Jiadun"、
  DISPLAY_FULL="Jiadun（价盾）"、SLUG="jiadun"；
- 任务书要求拼写为 "JiaDun"（大写 D）：与现 "Jiadun" 大小写不一致；
- 处置：本轮不改包名/仓库名（任务书允许），记录大小写差异为技术债/
  待用户定夺项；GUI 标题为 "Jiadun（价盾）— 工程经营合规智能工作台"，
  不存在 CostGuard 用户可见混用（内部兼容读取除外，已标注只读兼容）。
