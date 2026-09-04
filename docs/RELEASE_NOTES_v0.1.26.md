# Jiadun（价盾）v0.1.26 — 性能修复与缓存层预发行

## 本版定位

v0.1.26 是在 v0.1.25 基础上的性能修复与缓存层版本（结构版本 v53），
也是一个预览候选。长期平台战略保持不变：macOS Apple Silicon 仍是 P0 主平台，
Windows x64 仍是 P1。本版不标记为正式生产版。

## 主要变化

### Sheet 单元格摘要缓存（F-3 性能修复，schema v53）

- 迁移 v53：新增 sheet_cell_digests 缓存表。raw_cells 按 sheet_id 插入后
  不可变，摘要算一次落缓存即可复用；表可随时清空重建，不影响摘要语义。
- raw_cell_digest 查缓存命中即返回（0.07 秒 vs 全量重算 2.96 秒，42 倍提升）；
  miss 时计算并写入缓存。raw_cells 按 sheet_id 插入后不可变，
  缓存永久有效。
- **真实数据实测**：民权项目 7 文件导入后 raw_cells 239,404 格，
  refresh_all 从 278 秒/次恢复到秒级。

### Sheet 清单类型标注（上批 v52）

- 迁移 v52 新增 list_kind（boq_detail/measure_unit/measure_total/summary/
  non_business），仅是内容类型标注，不改变 sheet_status 门控语义。
- sheet_inventory.py：list_workbook_sheets 全工作簿 Sheet 清单+
  suggest_list_kind GB50500 特征建议+set_sheet_list_kind 人工标注。

### 继承能力

v0.1.25 及之前的合同确认生命周期、逐页对照复核、对上控制基准、费率规则等
全部保留。

## 验证结果

### 已通过

- 全量 pytest 以退出码 0 收口；ruff 通过；黄金回归合成 PASS；发布一致性
  检查 conditional；Windows x64 打包链；macOS arm64 CI 构建。

### 仍是 PENDING / NOT VERIFIED

- 真实工程扫描 PDF 的 OCR 质量回归；脱敏黄金样本登记仍为 0。
- 真实 Microsoft Excel/WPS（Windows 与 macOS）真机专项。
- 真实 Microsoft Excel（Windows Excel 与 macOS Excel）和 WPS（Windows WPS 与
macOS WPS）四环境真机专项。1万、5万、20万行完整导出基准的同环境实测；
Windows 代码签名与 macOS 公证。

## 升级影响

旧项目打开后自动迁移到 v53（迁移前自动备份）。缓存表可随时清空重建，
不影响业务数据。

## 安装与兼容

源码运行仍为 Python 3.12 + `uv sync`。下载前请核对同一 Release 的
SHA256SUMS.txt。不要将 PENDING / NOT VERIFIED 误读为已通过的真机能力。
