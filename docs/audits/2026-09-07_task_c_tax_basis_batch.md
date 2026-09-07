# 任务 C 批次报告：税口径模型（2026-09-07 夜间）

## 1. 本批目标

任务书任务 C（P0，用户反馈#4）：税口径结构化 + 表头明确文本自动识别 +
独立税金列登记 + Evidence + 人工改判 + 口径冲突降级。前置：今晚 23 点档
先补做了早上漏执行的 v0.1.28 桌面部署（哈希核验一致）。

## 2. 实际修改文件

| 文件 | 变化 |
| --- | --- |
| `src/jiadun/core/db/migrations.py` | 迁移 v55：raw_sheets 增 tax_basis/tax_basis_source/tax_basis_reason/tax_basis_updated_at/tax_basis_actor；line_items 增 tax_amount |
| `src/jiadun/core/engine/tax_basis.py` | **新增**：detect_tax_basis（明确文本识别）/ tax_basis_comparable（可比性判定）/ set_sheet_tax_basis（人工改判+Evidence） |
| `src/jiadun/core/parsing/header_detect.py` | 新增 tax_amount 字段模式（税金/税额/增值税额） |
| `src/jiadun/core/parsing/extract_items.py` | tax_amount FieldSource + NUMERIC_FIELDS + 落库 |
| `src/jiadun/core/engine/crosscheck.py` | 字段集纳入 tax_amount |
| `src/jiadun/core/engine/settlement_io.py` | 导入时按表头文本识别税口径写 raw_sheets（不覆盖人工标注）+ 识别 Evidence |
| `src/jiadun/core/engine/sheet_inventory.py` | 结转携带税口径全套字段；修复表头特征提示（col_map 值是列号，改用税口径理由提取提示） |
| `src/jiadun/core/contracts/run_contract.py` | sheet_scope 纳入 tax_basis/tax_basis_source；CONTRACT_FORMAT_VERSION 2→3 |
| `src/jiadun/ui/dialogs/sheet_browser.py` | 新增税口径列/依据列/下拉改判（选中行同步下拉防误改） |
| `tests/unit/test_tax_basis.py` | **新增** 15 个测试 |
| `tests/golden/cases.json` | run_contract.schema_version 54→55 |
| `docs/GOLDEN_BASELINE_CHANGELOG_v0.1.29.md` | **新增** |

## 3. 业务语义变化

无金额语义变化（不改变任何数值计算）。状态语义变化：

- Sheet 级税口径结构化：unknown（默认）/included/excluded；只有表头明确
  文本（含税/不含税/价税合计，负向后顾排除「不含税」里的「含税」子串）
  或人工标注能离开 unknown——「单价通常不含税」不自动认定（C1）。
- 独立税金列只登记 line_items.tax_amount 事实，不推断口径（C1/C5）。
- 口径变化（自动或人工）→ Run Contract 签名变化 → 旧运行失效（C6-10）。
- tax_basis_comparable：未确认→PENDING、不一致无转换依据→INCOMPARABLE
  （C4，供对上对下比较层使用）。
- 重解析结转携带人工税口径（与角色同规则）。

## 4. 数据库变化

- migration v55（6 条 ALTER，幂等容错沿用既有 duplicate-column 跳过）；
  旧项目打开自动迁移，迁移前自动备份；回退代码即可忽略新列。
- 合同格式 v3：旧签名全部失效并在下次校核重建（口径语义变化的有意行为）。

## 5. 测试结果

- 新增 `test_tax_basis.py` 15 个：明确不含税/含税/价税合计识别、税金列只
  登记事实、无税信息、税率不是口径、冲突标记、可比性三态、改判理由必填、
  改判 Evidence、外来 Sheet 拒绝、口径变化合同失效、结转携带——全过。
- ruff：通过（自动修 1 处）。
- Golden：passed（PASS=1，PENDING=1，基线 55）。
- 全量 pytest：**运行中，结果见交接补记**。
- 性能/Office/WPS/OCR：未验证。

## 6. 真实资料结果

本批未导入真实资料（语料库仍被扫描器阻塞，见 09-06 交接 §二十）。
C6 场景 6（Sheet 与合同税口径冲突）在合同事实与 Sheet 口径的自动比对层
尚未接线——当前以 tax_basis_comparable 提供比较语义，跨文档冲突检测排
下一批（如实登记为 PENDING）。

## 7. 已知风险

- 「税前/除税」等词在表头以外出现（如编制说明文字）不影响——识别只用
  表头行文本；但多层表头把「含税」写在单位行等边角时依赖表头行范围识别
  的准确性，误判可由人工改判兜底（Evidence 可追溯）。
- 导入自动识别不覆盖人工标注（tax_basis_source='human' 保护）。
- 全量回归存在既有慢测试（约 40 分钟），见 09-06 报告，待排查。

## 8. 回滚方式

- 本批提交：见 git log（任务 C 单批提交 + 版本提升提交）。
- migration 影响：v55 只加列，回退代码即可。
- 数据恢复：迁移前自动备份。

## 9. 下一批建议

1. C6 场景 6 补全：合同条款税口径 ↔ Sheet 口径的冲突检测进 Finding。
2. 任务 D 措施费数量语义（"数量视同1"人工勾选 + Evidence + 报告标注）。
3. 回归慢测试排查（95% 处约 40 分钟）。
4. 语料库：等待用户对扫描器阻塞拍板（09-06 §二十 报告）。
