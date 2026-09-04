# Jiadun（价盾）当前仓库真实性审计

> 审计日期：2026-09-02（Asia/Shanghai）
> 审计对象：当前 main、工作树与 GitHub 远端状态
> 审计目的：为“可信候选版本”下一批最小改动建立可复核基线，不把 Preview 阶段能力表述为生产结论。
>
> 版本说明：本文主体保留 v0.1.16 发布前的审计快照，后续 v0.1.17 阶段性预发行的
> 当前定位、验证结果和未闭合门槛以 `docs/RELEASE_NOTES_v0.1.17.md`、README 和
> `scripts/release_checklist.py` 的现场输出为准；历史快照中的“尚未发布”表述不代表
> v0.1.17 发布后的远端状态。

## 1. 审计范围与不可变边界

本文件先记录了 2026-09-02 的只读盘点，随后在同一工作树继续执行了经确认的 P0 信任加固，并依据独立复核逐项补上发布闸门、资产来源和恢复路径修正：本轮又补充了源工作簿 Sheet 目录盘点、解析结果索引/网格预校验、导入物化外层事务、嵌套导出路径隔离、性能报告输入防御和黄金注册表错误结构化返回。在该审计快照截点没有修改结算口径、数据库迁移、演示源文件或私有原始副本，也没有创建 Release；新增/修改内容当时仍在本地未提交工作树。审计范围包括项目约定、架构/路线/变更说明、双语 README、pyproject.toml、src/jiadun/、tests/、scripts/、当前黄金注册表和发布检查入口。

必须继续遵守：

- 原始文件和私有测试副本只读；local_private_data/ 不进入 Git。
- 金额、数量、单价使用 Decimal；缺失值不补 0，不可比数据不强行比较或调平。
- 当前结果受 Run Contract、Evidence 和四级状态闸门约束；证据不足时保持 fail-closed。
- AI 不承担金额计算、最终匹配、责任判断或正式结论。

审计基线时工作树只有一个预先存在的未跟踪文件 `uv.lock`，本次未处理、未删除、未提交它。随后候选修复新增/修改了本文件、设计规格、版本入口、验收/黄金/发布脚本及回归测试；这些当前改动尚未提交或发布，不能与远端 `main` 的已发布内容混同。

## 2. 审计时的代码与远端真实状态（v0.1.16 快照）

| 项目 | 当前事实 | 证据/状态 |
| --- | --- | --- |
| 当前分支 | main 与 origin/main 对齐 | 51f1c2676c7acffbadb96ebb55f2b6b3953d56a0 |
| 最近提交 | docs: state COM verification scope honestly in v0.1.16 notes | 2026-09-02 09:29:28 +08:00 |
| Git 描述 | v0.1.16-2-g51f1c26 | 当前提交在 v0.1.16 标签之后 |
| GitHub 最新发布 | v0.1.16，预发行 | Jiadun（价盾）v0.1.16 — 真实资料实测收口与异常定位预发行 |
| 最新 CI | main=51f1c267，完成且成功 | GitHub Actions CI run 33579598931 |
| 发布定位 | Preview/Prerelease | 未签名/未公证、真实案例和 Office/性能现场门槛仍未闭合 |

远端已经有 v0.1.15 Windows x64 安装/便携包和 v0.1.16 异常定位、魔数拒绝、公式告警降级；当前主线没有把这些阶段性功能宣传为正式生产能力。

## 3. 版本真源与一致性核对

### 3.1 已核实的来源链

| 位置 | 读取方式 | 结果 |
| --- | --- | --- |
| pyproject.toml | [project].version | 0.1.16 |
| 统一运行时版本入口 | src/jiadun/version.py::app_version() | 开发树唯一读取仓库 `pyproject.toml` 的 `[project].version`；源码清单不可用时才只读回退 jiadun/costguard metadata |
| Run Contract | src/jiadun/core/contracts/run_contract.py::_app_version() | 委托统一 `app_version()`，不再维护独立正则/回退顺序 |
| Excel/Word 导出 | src/jiadun/core/export/excel_export.py 调用 run_contract._app_version() | 与 Run Contract 采用同一运行时版本读取函数 |
| 私有验收报告 | scripts/real_acceptance_run.py::jiadun_version() | 委托统一 `app_version()`；当前源码工作树记录 0.1.16 |
| 发布一致性/清单 | scripts/release_consistency_check.py::_read_version()、scripts/release_checklist.py::_version() | 委托 `read_project_version(root)`，与源码 TOML 解析保持一致 |
| macOS 构建 | scripts/build_macos_arm64.sh 读取 pyproject.toml，注入 JIADUN_VERSION；spec 写入 bundle 版本 | 当前脚本链一致 |
| Windows 构建 | scripts/build_windows_x64.ps1 读取 pyproject.toml，注入 JIADUN_VERSION；Inno Setup/portable 名称使用该值 | 当前脚本链一致 |
| wheel | Hatchling 从 pyproject.toml 生成 jiadun metadata | 当前源码版本为 0.1.16 |
| 文档 | README、架构、路线、变更说明、v0.1.16 release notes | 当前版本标记均含 0.1.16 |

### 3.2 自动检查结果

`release_consistency_check.py --json --allow-no-real` 返回 `ok=true`、源码/文档版本均为 0.1.16，未发现当前文档仍宣称 v0.1.11 的事实。生产放行仍为 `production_release_ready=false`：没有可用脱敏真实黄金案例，且四环境/性能等现场门槛未闭合；`--allow-no-real` 只是开发阶段有条件检查，不是生产放行。黄金发布门禁现在严格校验 canonical `comparison_status_counts`、登记表类型、案例版本、输入 SHA-256、资产目录和真实案例 provenance；性能清单内外两条路径共用严格校验，要求三规模、唯一阶段、现场导出文件存在且字节数/SHA-256一致，不能再由 `all([])` 或自报哈希形成绿色状态。

当前不存在“pyproject=0.1.11、README=0.1.11”的交接文档所述漂移。版本入口已收敛到 `src/jiadun/version.py`，并由 `tests/unit/test_release_consistency.py` 锁定 Run Contract、验收脚本、发布清单和发布一致性读取结果一致；DMG/Windows 构建脚本仍直接读取同一 `pyproject.toml` 字段，未另设手工版本常量。

## 4. 当前自动化证据

以下命令在基于 `main=51f1c267` 的当前本地工作树执行；代码和文档候选仍未提交：

| 检查 | 结果 | 解释 |
| --- | --- | --- |
| 全量 pytest | **738 passed** | 本轮完整回归通过；不等同于真实项目或四环境 Office 通过 |
| 本轮针对性测试 | **通过** | 覆盖黄金路径穿越/越界软链接、默认金额口径 fail-closed、非对象性能报告、畸形阶段结构、嵌套同名导出回滚、语义不完整/错身份 marker、源 Sheet/范围漏读、重复/跳号 Sheet 索引和覆盖证明失败事务回滚反例 |
| Ruff | **All checks passed** | 静态检查通过 |
| git diff --check | 通过 | 无空白错误 |
| generate_demo_data.py --check | 连续 3 次通过 | 合成演示文件字节确定性稳定 |
| synthetic golden | 可用案例 1 个，差异 0 | 只证明合成管线可执行 |
| sanitized real golden | 0 个可用案例 | sanitized_real_template 为 not_available |
| release consistency | `--allow-no-real` 为 ok=true；默认门禁返回 1 | production_release_ready=false，真实案例门槛仍未满足 |

性能现场证据（均为仓库外 `/tmp` 合成数据工作区）如下：

- `/tmp/jiadun-performance-20260902/runs/20260902_131756_21b2a0b9/performance_benchmark.json`：1 万行全链路（含 Excel 导出）完成；5 万行完成至 A/B/C，Excel 导出因耗时过长由人工安全取消；顶层状态为 `cancelled`，不能作为三规模通过。
- `/tmp/jiadun-performance-20260902-200k-core/runs/20260902_133835_12c27bfe/performance_benchmark.json`：20 万行核心导入在取消前未完成，`results=[]`；`skip_export=true` 仅用于观察导入上限，不能作为生产性能证据。
- 两次取消均保留现场、未产生半成品 Excel；发布清单当前把上述报告判为 `conditional`，不会判为 `passed`。性能完整三规模、取消/恢复和导出现场门槛仍未闭合；当前 validator 还会在可报告的完成现场中重新读取导出文件、核对字节数与 SHA-256，并拒绝重复阶段或跨现场路径。
- 当前发布清单现场文件为 `/tmp/jiadun-release-checklist-20260902-latest/release-checklist-v0.1.16.json` 与同名 Markdown：自动化测试、Ruff、迁移、备份恢复和异常退出测试均为 `passed`；黄金为开发覆盖 `conditional`，性能为 `not_run`，Office 为 `conditional`，签名为 `not_available`，总体 `conditional`，`production_release_ready=false`。
- 当前工作树已重新构建本地 Apple Silicon 测试包 `dist/Jiadun-0.1.16-macos-arm64.dmg`：arm64 原生、最低 macOS 15.0、ad-hoc 签名、DMG 挂载自检通过；本次构建 SHA-256 为 `b0ff0c843081d2df1551c9968eacaab7ba7e4bc94683c26164da1b114610cc10`（89,537,268 bytes）。通过桌面冒烟检查确认首页为空白，点击“导入资料文件…”可打开原生 Mac 文件选择器并选中示例 XLSX，随后取消并确认测试包退出；这不是完整真实项目或 Office 现场验收。

合成黄金当前可观察到 6 个期次均为 verification_level=insufficient，并记录缺失值、异常和 Evidence；这符合 fail-closed 原则，不能把合成通过写成项目级结论。性能基准的 write-only 合成工作簿在源 XML 缺少 dimension 时会由独立 row/c 扫描推导范围，因此当前三个性能单元测试可执行；这仍不等于 1 万/5 万/20 万行真实完整现场已通过。

## 5. Issue #4/#5/#9 当前主线复现

三个 Issue 在 GitHub 上当前仍为 **OPEN**。变更说明中的“已修复”不能替代当前主线测试，因此本审计不直接关闭它们。

### #4：A/B 同源风险与 C 缺失

- 代码路径：A 从 line_items 汇总；B 从不可变 raw_cells 重新扫描；C 从原表控制单元格读取。Evidence 记录文件、Sheet、物理行范围、过滤条件和路径独立性字段。
- 当前定向测试已覆盖干净路径、重复明细、负数调整、缺失金额、C 控制来源定位、多个合计候选、范围未证明和部分覆盖等 fail-closed 反例。
- `tests/unit/test_p0_trust_gates.py::test_line_item_mutation_cannot_contaminate_raw_b_path_or_green_status` 已把上述污染场景固化：在 500.00 基线中把一条 `line_items.amount` 从 200 改为 9999.99 后，A 变化、B 仍为 500.00，结果为 `status=diff` 且 `verification_level` 不足。这是当前 main 上的命名回归证据，不再只是一次性探针。
- 当前结果还同时保留 `ab_independence_level=shared_extractor`（覆盖证明本身的历史事实）和 `path_independence_level=source_independent_raw_scan`（本次 B 的实际路径）；界面/导出和新增测试均把两者分开，避免把 A/B 同源覆盖证明误读为完整独立性。

**状态：未关闭。** 代码回归已补齐，但真实工程黄金案例、跨输出读取面和现场证据仍未闭合；Issue 仍不能仅凭合成测试关闭。当前候选还额外覆盖了处理后源副本变化时结果失效的 fail-closed 回归。

### #5：人工确认工作流可达性

- 当前 MainWindow 支持文件、多选、文件夹和拖拽入口；导入后工作台会提示待确认数量并可打开 SheetConfirmDialog。
- 当前对话框测试覆盖预览、候选字段映射、表头/数据范围、方向/期次、按清单抽取、仅存证、原因必填和审计记录；相关 UI 定向测试通过。
- 当前证据是 offscreen 自动化，不是真实用户在 Mac 上的三步点击记录；没有把测试通过表述为 Mac/WPS/Excel 真机通过。

**状态：未关闭。** 需要在真实 Mac 应用包上再走一次 needs_manual_review 文件的人工流程，并将结果记录为可复核验收证据。

### #9：Windows 生成器锁定与 ZIP 元数据

- _normalize_zip 对 os.replace 使用 6 次递增退避重试，并固定 ZipInfo.create_system=3。
- 当前测试覆盖仓库演示包元数据、重新归一化后的 create_system，及跨进程字节确定性；演示检查连续 3 次通过。
- 当前候选还修复了隐私审计脚本在 Windows cp1252 控制台输出中文时的编码崩溃，新增
  cp1252 输出流回归测试；当前环境不是 Windows，未模拟杀毒软件文件锁，也未进行
  当前版本 Windows 原生重建，静态代码和跨平台合成测试不能替代 Windows 现场证据。
- 远端历史 Windows 打包运行 `33303404713` 的失败日志已确认该编码根因
  （`UnicodeEncodeError`，cp1252 无法编码中文审计提示）；当前主线尚未在修复后
  重新执行 Windows 打包，因此 Issue #9 仍保持 OPEN。

**状态：未关闭。** 保留 Issue，待 Windows CI/实机记录覆盖短暂锁定重试后再决定是否关闭。

## 6. 黄金回归与私有资料现状

### 6.1 公开合成层

- examples/demo/：4 个合成文件、manifest、SHA-256 和中文说明；生成器固定时间/ZIP 元数据，不能包含真实项目资料。
- tests/golden/cases.json：1 个可用 synthetic 案例和 1 个明确 not_available 的真实模板。
- scripts/golden_regression.py：只读比较，不自动更新 expected baseline；当前合成案例差异为 0。
- scripts/matching_benchmark.py：只读评价人工标注的稳定项目项身份，按项目项对
  输出 precision、recall、F1、误报/漏报具体项目对，并统计自动确认、高概率、疑似、
  不可比、待补资料和人工复核数量；缺少真值保持 `PENDING`，标签或预测身份不能对齐
  时保持 `INCOMPARABLE`。当前尚无真实案例真值，不能把该工具输出解释为匹配质量通过。

### 6.2 授权脱敏/私有层

- tests/anonymized_golden_cases/cases.json 当前只有 sanitized_real 的 not_available 占位案例，真实黄金预期数量仍为 0。
- local_private_data/real_acceptance/manifest.csv 当前登记 18 个测试编号：R01–R13 副本存在，T14–T18 副本在当前本地工作树缺失。
- 已按本候选脚本重新运行本地私有验收：`work/run_20260902_200703_648474/LOCAL_ACCEPTANCE_REPORT.md`；本轮预检仍明确为 `pending`，R01–R13 继续执行，T14–T18 各自生成 `pending_source_data` marker，未把缺失副本当作空数据。运行后 `before_all_match=false`、`after_all_match=false`、`modified_copies=[]`，不存在把缺失项误报为篡改项；R01–R13 的技术状态与上一轮一致，未因当前候选改动而放宽结论。
- 本次没有连接、复制或读取用户指定的外部移动硬盘，也未补取 T14–T18；该位置只作为后续候选数据源，待明确授权、脱敏副本和哈希 manifest 后再接入。
- 现有 local_private_data/real_acceptance/acceptance_results.json 和 acceptance_report.md 是 2026-08-29 的旧 13 文件记录，结构和版本不能作为当前 v0.1.16 的完整黄金回归证据。它们只作为历史现场保留。
- 四环境 WPS/Excel 当前没有可重复的完整矩阵记录。docs/WPS_ACCEPTANCE_STEPS.md 只证明 2026-08-29 的合成最小样例和有限人工步骤，不能扩展为 R01–R13 或 v0.1.16 全部通过。

## 7. 尚未闭合的生产门槛

本候选已经关闭“代码没有回归保护”的一部分缺口，但不等于生产放行。仍需补齐：

1. 真实工程黄金案例：授权、脱敏、哈希、expected baseline、匹配/异常/Evidence 逐项固化；当前真实可用案例数仍为 0。
2. 真实资料覆盖范围：T14–T18 源副本缺失；A/B/C 破坏性测试虽已覆盖主要合成反例，仍需在授权真实案例上复现并复核。
3. macOS WPS、macOS Excel、Windows WPS、Windows Excel 的打开、重算、筛选、打印、Evidence 跳转、保存后重读现场记录。
4. 1 万、5 万、20 万行完整性能、进度、取消、异常恢复、导出半成品清理现场记录；本轮已形成可追溯现场报告，但 5 万行导出和 20 万行导入均取消，尚无三规模完整通过证据。
5. 签名、公证、发布 CI 完整性、备份/恢复、迁移失败回滚和异常退出恢复的当前版本证据。
6. 跨独立项目库的授权知识复用仍不能宣传为企业级全局知识中心。

本候选新增的代码门禁证据包括：副本预检/处理后哈希失效、验收阶段异常 marker 与失败重试、黄金 canonical 状态及非法计数阻断、登记表类型/版本/来源目录/provenance/输入 SHA-256、黄金资产解析后目录 containment 与软链接拦截、性能报告内外路径严格 schema 与现场文件复核、成对导出失败和嵌套同名半文件清理、源工作簿 Sheet 目录与解析结果独立对照、金额单位未确认时项目级降级、方向期次数量勾稽、历史报告不覆盖，以及统一版本入口；这些证据不能替代上述外部环境和真实业务资料证据。

## 8. 下一批最小实施构想（不扩大业务范围）

继续坚持“可信度优先、无新功能扩张”，建议按以下顺序推进：

### 批次 A：授权真实资料接入

明确允许读取后，从外部移动硬盘只复制到本地 `local_private_data/real_acceptance/corpus/` 的脱敏副本；逐文件生成 SHA-256、只读 manifest 和人工确认的 expected 结果。原件、客户名称、合同编号和私有路径不进入 Git 或公开黄金库。

### 批次 B：真实黄金回归与独立复核

逐案例补齐控制总额、明细行数、A/B/C、匹配、异常、不可比项和 Evidence；保持 `PASS/FAIL/PENDING/INCOMPARABLE` 闭世界比较，任何差异先解释再人工批准，不自动更新 baseline。

### 批次 C：外部环境证据

在真实代码门槛通过后，再安排 Mac/Windows WPS/Excel、1/5/20 万行、取消恢复和打包签名验证。没有现场软件或机器时，报告只能保持 PENDING，不得用自动化或合成结果代替。

本构想不包含 AI 自动结论、云同步、多人协作、BIM/CAD、ERP、复杂仪表盘或市场价格预测。

## 9. 审计快照结论（截至 v0.1.16）

独立复核第一轮指出性能空结果 false-green、可用黄金输入缺失 SHA-256、成对导出失败残留、损坏 marker/人工决定文件中断等问题；后续复核进一步构造出性能伪造导出/重复阶段、合成案例冒充真实、失败 marker 不重试、资产路径穿越、解析器共同漏 Sheet、默认金额口径缺省放行、嵌套同名导出误删、Sheet 索引错配和导入非原子等反例。本地候选已按这些反例补充实现和测试，并继续增加“Sheet 尺寸不变时的有值单元格坐标指纹漏行检测”、导入/人工确认失败的具体原因和重试提示；当前候选已增加“覆盖证明阶段失败时整体回滚”“完整性能报告阶段结构闭合”和“新导入项目首次分析前物化 `period_totals`”的可复现证据，并完成 **738 项全量回归**。当前远端 `main=51f1c267` 仍是 **Jiadun（价盾）v0.1.16 预发行候选**；本地工作树包含尚未提交的 P0 信任加固候选，版本仍为 0.1.16，没有创建 Release。代码自动化证据已经增加，但 Issue #4/#5/#9 均缺少足以宣称“真实生产关闭”的当前主线/现场证据。真实黄金案例数量为 0；私有验收本轮明确保留 T14–T18 为 PENDING；Office 四环境、三规模完整性能、签名公证和恢复门槛未闭合，当前候选尚未达到生产发布条件。

因此，截至该审计快照不适合标记为正式生产版、关闭高影响 Issue 或发布新版本。后续 v0.1.17 候选的当前定位、验证结果和远端发布状态不由本节回写，统一以对应发布说明、README、发布清单和 GitHub Release 为准。

## 10. 2026-09-03 当前工作树增补：页级混合 PDF/OCR（v0.1.21 预发行候选）

本节不改写上方历史审计，只补记当前继续开发时的可复核状态。当前工作树基线为：

- 分支：`develop/v0.1.20-import-intake`；HEAD：`0fd9ef539ad9ed12587d73b075ce13ce93f34824`；
  开发前远端 `v0.1.20` 为预发行版本；本次目标为 `v0.1.21` 预发行候选。
- 开发前只读检查已完成：`ruff` 通过、依赖锁定检查通过、合成黄金回归为
  `PASS` 且真实黄金案例为 `PENDING`；工作树原有的 AGENTS.md 用户修改未覆盖。
- 已确认的原代码问题：混合 PDF 只要存在一个文本页，旧 `parse_pdf` 就会跳过无文本层页，
  仍可能生成“解析成功”的不完整合同事实；旧路径没有逐页覆盖状态和 OCR Evidence 元数据。
- 当前修复：新增跨平台页级 PDF 管线和 `platform/ocr.py` RapidOCR 适配；每页严格记录状态，
  页缺失/错序/多页和 OCR 不可用均 fail-closed；摘要复用现有 `parse_batches.stats_json`，
  不新增数据库迁移；OCR 合同候选标记 `needs_review` 并排除出当前 Run Contract。当前又补齐
  OCR 候选风险分析门控、失败重试时旧合同风险的 Evidence/Finding 历史化、批次快照严格复用
  校验、锁定模型版本/文件哈希，以及 UI 对 `needs_review` 的部分导入提示；混合文本层+图片
  页面直接进入人工复核，拒绝仅依赖文本层。旧同步兼容入口对合同/纪要只登记为待人工
  分类，不再把未分类资料解析为合同事实；Run Contract 和合同风险读取面也按合同类别
  fail-closed。OCR 批次复用要求固定 RapidOCR 版本及三份模型的 canonical 清单，资料
  重新分类写入审计前后值、运行绑定并刷新全部工作台读取面。
- 针对性验证已通过：页级 PDF/OCR 测试 17 项、合同提取/运行契约测试共 47 项（1 项按环境
  跳过）、资料中心/UI 导入测试共 18 项（3 项按环境跳过），Ruff、`uv lock --check` 通过；
  不含超长性能基准的完整集合共收集 818 项并以退出码 0 完成；本机
  RapidOCR 1.4.4 三份随包模型的固定大小/ SHA-256 校验通过。Windows onedir 资源收集烟测
  已确认 `Jiadun.exe` 和 3 份 ONNX 模型在产物中，但不是最终包启动或真实 Office 验证。
- 当前验证边界：新增单元/集成测试使用临时 PDF 占位文件、假渲染器和假 OCR provider；本机
  随包 RapidOCR 对临时中文扫描页的真实渲染链验证已通过。真实项目 OCR、Windows 打包后
  最终包启动与取消/异常恢复、真实 Microsoft Excel/WPS、macOS Excel/WPS、脱敏黄金样本和多种扫描质量仍为
  `PENDING / NOT VERIFIED`，不能由上述测试替代。
- 原始业务资料：本次页级管线测试未读取用户真实项目文件；所有测试文件均在临时目录，
  不进入 Git。若后续使用真实样本，必须先复制到独立临时目录并在前后比较 SHA-256、大小和
  修改时间，任何原件哈希变化立即停止。

## 2026-09-04 v0.1.23 收口记录（ZCode 接力）

- 接力背景：v0.1.21/v0.1.22 在交付窗口压力下发行，P0 修复后的全量 pytest 中止于约 8%
  记为 PENDING；四个发行（v0.1.19–v0.1.22）均无附件、CI 未运行、main 未推进。
- 全量回归收口：Windows x64 / Python 3.12 项目环境 `python -m pytest -q` 完整运行，
  最终退出码 1，818 项收集，5 个失败全部集中发布门槛测试：
  - `test_release_consistency.py` 2 项：测试硬编码 `expected_version="0.1.20"`，
    与 pyproject `0.1.22` 不符（测试断言过期，非业务回归）。
  - `test_release_checklist.py` / `test_release_consistency.py` 3 项：v0.1.22 升版时
    未建 RELEASE_NOTES、未更新 README/ROADMAP/ARCHITECTURE，发布一致性检查按设计
    判 failed（门槛生效的证明，不是误报）。
- 修复：版本号升至 0.1.23；README / README_zh-CN / ARCHITECTURE / ROADMAP 当前版本
  状态更新；新建 `docs/RELEASE_NOTES_v0.1.23.md`（含预览候选定位、门槛清单与
  PENDING 边界）；门槛测试期望版本改为读 `pyproject.toml`（`tomllib` /
  `release_checklist._version`），消除每次发版手改测试的隐患。
- 黄金回归：`scripts/golden_regression.py --json` 实跑 `status=passed`，
  合成案例 `PASS 1`；真实案例 `not_available/PENDING`（无脱敏登记，不冒充）。
- 业务计算边界：本轮零业务代码改动（仅版本号、发行文档、测试断言来源）；Decimal /
  Run Contract / Evidence / OCR 页级管线均未触碰。
- 仍然 PENDING / NOT VERIFIED：真实工程扫描 PDF OCR 质量、OCR 合同候选人工确认
  界面、对上控制候选上限/冲突算法、真实 Microsoft Excel/WPS（Windows 与 macOS）
  真机、最终包启动与取消/异常恢复、50k/200k 完整导出基准、Windows 签名与 macOS 公证、
  脱敏黄金案例登记（当前为 0）。
- 原始业务资料：本轮未读取用户真实项目文件；全部测试使用仓库合成/临时数据。

## 2026-09-04 v0.1.23 增补：macOS 临时目录 symlink 缺陷与 Windows 长路径构建修复

- macOS CI（Package macOS arm64 @ v0.1.23 tag）实测暴露：系统临时目录位于 symlink
  之后（`/var→/private/var`）时，`backup_project`/`verify_backup` 的暂存目录
  （`tempfile.TemporaryDirectory(prefix="jiadun_backup_*"/"jiadun_bverify_*")`）与
  `golden_regression` 的工作区会被 v0.1.19 引入的 fail-closed 链检查按
  "路径包含 symlink" 拒绝。Windows 开发/测试环境 temp 前缀无 symlink，因此本地
  全量回归未能暴露此平台差异。
- 修复：在创建处 `Path(...).resolve()` 解析到真实路径（`backup_restore.py` 两处、
  `golden_regression.py` 一处）；不放松任何检查强度，用户提供的路径若含 symlink
  仍按原样 fail-closed。新增回归测试
  `test_backup_survives_system_temp_behind_symlink`（monkeypatch tempdir 指向
  symlink 目录，Windows 无 symlink 权限时按仓库既有守卫跳过，macOS CI 真实执行）。
- Windows 本机构建实测暴露独立问题：仓库根路径过长时，Inno Setup 以未折叠的
  `..\..\..\..` 相对前缀拼接内部路径，超出 Windows 260 字符上限，压缩中段报
  "系统找不到指定的路径"。构建脚本 Inno 步骤改为在仓库路径过长时用 subst 短盘符
  调用 ISCC（用后即删），产物路径与校验流程不变。
- 本修复随 v0.1.23 tag 交付；Windows 安装包/便携包从含本修复的最终提交重建。

## 2026-09-04 v0.1.23 增补 2：main CI Windows 作业英文 locale 编码修复

- main 推进后 CI 首次覆盖 v0.1.19–v0.1.22 代码，`test-windows-x64` 作业暴露 4 项失败
  （本地中文 locale 全量回归为绿，未暴露）：cp1252 控制台下中文输出/写入
  UnicodeEncodeError。`test_macos` 与安全扫描作业通过，页级 OCR 修复在 macOS CI 实测有效。
- 修复：`test_acceptance_runner.py` / `test_backup_restore.py` 两处 `write_text` 补
  `encoding="utf-8"`；`generate_demo_data.py` / `release_consistency_check.py` 的
  `main()` 入口对 stdout/stderr 做 UTF-8 reconfigure（对英文 Windows 真实用户同样是
  产品级修复）。本地以 `PYTHONIOENCODING=cp1252` 复现验证通过，ruff 通过。

## 阶段 C-1 进行中：合同事实确认生命周期（schema v48）

- 依据宪章"只有 confirmed 才能用于金额和控制规则"与 ROADMAP 阶段 C，本轮实现确认
  生命周期基础设施：迁移 v48 为 contract_facts 增加 review_status/reviewed_at/
  reviewed_by/review_reason；历史事实一律回填 candidate（无法证明人工确认不得视为
  已确认，宪章原则）。
- 新增 set_fact_review/list_contract_facts：确认/拒绝/待复核三向流转，推翻已确认或
  已拒绝结论必须填写理由；每次流转写入 contract_fact_review Evidence（前后值、操作者、
  理由），随后按既有机制产生新的 Run Contract 签名（旧运行保留为历史）。
- Run Contract：事实载荷带确认状态标记；被人工拒绝的事实不再进入运行契约；
  新增 contract_fact_review_summary 汇总。风险语义：拒绝条款视为缺失；候选条款
  保持覆盖不误报缺失，但给出"候选条款待人工确认"低级提示（fail-closed 可见，
  不静默当作已确认）。
- UI：工作台新增"合同条款确认…"对话框（状态筛选、确认/拒绝/待复核、推翻理由必填），
  offscreen UI 测试覆盖列表/确认/阻断提示。
- 行为影响（如实声明）：v48 迁移后所有存量合同的 Run Contract 签名将变化（载荷新增
  状态字段），已存在的计算结果保留但退出当前结论，用户重跑后恢复当前；候选语义不
  改变既有风险判定（候选仍算覆盖），不存在存量项目风险突增。
- 验证边界：本阶段为纯增量；尚未实现 OCR 扫描页逐条对照复核入口与 confirmed 接入
  控制基准候选算法。全量回归结果见本轮记录。

## 阶段 C-2 进行中：PDF 逐页人工对照复核（schema v49）

- 依据宪章"含 OCR 页的合同停在 needs_review、候选不进入运行契约"与 ROADMAP 阶段 C，
  本轮补上把文档救回来的合法路径：逐页人工对照复核。
- 迁移 v49 新增 pdf_page_reviews（project_id/file_id/page_number 唯一，保存当前决定，
  历史流转留 Evidence）。list_pdf_pages/set_page_review/mark_document_pages_reviewed：
  页清单与复核状态、单页核实（必填对照依据）/退回、全部应复核页 verified 后文档
  needs_review → parsed（写 Evidence，产生新 Run Contract 签名）。
- fail-closed 边界：无 PDF 批次/覆盖不完整拒绝复核；原生文本页无需也不可复核；
  pending_ocr/failed 文档不适用（需先重新解析）；未核实全部应复核页不得完成。
- UI：资料中心"逐页对照复核…"对话框（页表/该页候选条款与原文引用/核实/退回/完成）。
- 语义边界：页级复核只解除文档门控；条款仍为候选，需按条款逐条确认（C-1 不变）。
  原件保持只读；OCR 全文不落库（沿用既有设计）。

## 对上控制基准候选与上限比较进行中（schema v50）

- 依据宪章 §六（reference/control_candidate/settlement_result 三角色、五态输出、
  禁止自动挑选基准）与用户业务需求（终审/审计报告作为对上对比上限），实现：
  - 迁移 v50 新增 control_baselines 表（amount 为 Decimal 字符串，tax_basis/
    scope_note/supersedes_id/确认状态与依据）。
  - 候选来源两条路：已确认合同事实（非 confirmed 拒绝；非金额值拒绝）与人工显式
    登记（出处必填）；候选确认必须给出核对依据；supersedes 必须显式声明。
  - compare_upward_result 五态：CONTROL_CONFLICT（有效基准并存且无替代关系）、
    INCOMPARABLE（税口径/范围明确不同）、PENDING（税口径未确认等）、
    FAIL（超出金额，附差额，不认定违规责任）、PASS。全部写审计 Evidence。
  - list_upward_periods：对上期次明细合计 = line_items.amount 的 Decimal 求和
    （导入管线已排除小计行），税口径取期次 tax_mode。
- UI：工作台"对上控制基准…"对话框（候选登记/确认拒绝/选期次比较/结论与差额展示）。
- 边界（如实）：控制基准尚未写入 Run Contract 载荷（比较为独立分析层，结果留
  Evidence）；范围比较目前是文本说明比对（双方都填写且不同才 INCOMPARABLE），
  结构化 scope 字段留待后续；框架/管理性协议独立算法仍为实现。

## 框架/管理性协议费率规则进行中（schema v51）

- 依据宪章 §五（费率不是一个百分比）与用户业务需求（协作方收 3~5 个点的框架协议
  独立算法），实现：迁移 v51 rate_rules 表；句子级触发词扫描 + 句内多百分比各成
  候选 + 公式型比例出"需人工解读"候选；确认必须人工设定 base_type/base_definition
  （结构化基数），公式型必须人工填比例；apply_rate_rule Decimal 确定性试算
  （round2，cap/floor）；全部写审计 Evidence；工作台费率规则对话框。
- 真实语料验证：民权框架协议终稿（66KB docx，隔离副本）实测 140 段抽出 19 条
  候选（18 数值 + 1 公式型），唯一比例 {0.2,3,4.9,5,9,10,11,13.03,22,50}%，
  与人工核对一致（11% 管理费分劈/4.9% 采保费/13.03% 分包利润率/0.2% 科研安全费）。
  首轮发现 4 个缺陷（公式句重复候选、公式型漏检、触发词缺口、全角％不支持）
  均已修复并复验。原件未动（隔离副本，测试前后哈希一致）。
- 边界（如实）：费率试算目前接受人工输入基数；基数与对上/对下期次合计的自动
  关联待接入；费率规则未写入 Run Contract 载荷。

- 发行决策记录（09-04 08:30）：v0.1.25 Windows 构建采用 -SkipChecks（跳过本地
  ruff+全量测试环节），理由：①同一内容树于 08:05 本地全量回归以退出码 0 通过
  （其后仅版本号与发行文档变化）；②main CI 正在同一提交 10f1219 上并行运行
  Windows/macOS 双平台全量 pytest；③GitHub Release 仅在 main CI 绿后创建——
  CI 即质量门槛。PyInstaller/PE 校验/隐私审计/安装器/SHA256 环节全部保留。
