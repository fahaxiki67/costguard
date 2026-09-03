# Jiadun（价盾）v0.1.23 — 页级 OCR 回归收口与发行卫生修复预发行

## 本版定位

v0.1.23 是在 v0.1.22 基础上的回归收口与发行卫生修复版本，也是一个预览候选。
长期平台战略保持不变：macOS Apple Silicon 仍是 P0 主平台，Windows x64 仍是 P1；
共享业务核心保持跨平台，Windows 特有逻辑继续放在 `platform/` 层。
本版不标记为正式生产版。

v0.1.21/v0.1.22 在交付窗口压力下发行，跳过了发行文档与一致性门槛、未重跑 P0 修复后
的全量回归，且四个发行均未附安装包。v0.1.23 不新增业务功能，专门把这些欠账补齐：
以 P0 修复后的真实全量回归结果作为版本记录依据，恢复“发行必须通过自身一致性检查”
的门槛纪律。

## 主要变化

### 回归收口（本轮核心）

- P0 修复（历史 `contract_docs` 缺少 `document_intake` 时 fail-closed，不进入
  Run Contract / `contract_risks`）之后的全量 pytest 以最终退出码重新收口；
  失败项逐一定位，全部为发行卫生问题（见下），无业务计算回归。
- 黄金回归重跑：合成案例 `PASS`；脱敏真实黄金案例仍为 `PENDING` 登记，
  不用合成数据冒充真实案例。

### 发行文档与一致性门槛修复

- 版本号 `pyproject.toml` 升至 `0.1.23`；README / README_zh-CN / ARCHITECTURE /
  ROADMAP 的当前版本状态更新至本版，并新建本发行说明，使发布一致性检查可通过。
- ROADMAP 新增 `v0.1.23 生产门槛（未全部满足）` 章节，逐项列出已关闭与未关闭门槛。
- 修复 5 个因版本硬编码而过期的门槛测试：期望版本改为从 `pyproject.toml` 读取
  （`test_release_consistency.py` 的 `_pyproject_version()`、
  `test_release_checklist.py` 的 `benchmark_version` 取当前版本），
  每次发版不再需要手改测试。

### 继承自 v0.1.20→v0.1.22 的能力（本版未改动，随版交付）

- 逐页 PDF 提取边界：按页覆盖 `1..N`，native_text / ocr / pending_ocr / ocr_failed /
  needs_review 页状态；缺页、错序、重复页、额外页和 OCR 失败一律 fail-closed，
  混合 PDF 不再因存在文本页而静默丢弃扫描页。
- 离线 RapidOCR 平台适配：随包三份 ONNX 模型校验大小、SHA-256、版本、语言与许可证
  元数据；不静默下载模型、不上传用户文件；OCR 合同候选标记 `needs_review`，
  人工复核前不进入当前 Run Contract。
- 资料中心：导入前显式选择资料类别（对上合同、对上终审/审计/审核报告、
  框架/管理性协议、会议纪要、对上收款台账、对下支付/结算台账等），
  方向/状态/来源副本/SHA-256 可追溯；未分类合同只登记不解析。
- 备份恢复安全（v0.1.19）：拒绝路径穿越、盘符/UNC、重复条目、manifest 不一致和
  symlink/junction/reparse point；Windows Excel/WPS 工作簿所有权保护。

## 验证结果

### 已通过（本版实际执行）

- Windows Python 3.12 x64 项目环境全量 `pytest`：P0 修复后重新以最终退出码收口，
  定位出的 5 个失败均为发行卫生问题并已修复；修复后相关门槛测试集合重跑通过。
- `ruff check src scripts tests` 通过；`uv lock --check` 通过。
- 黄金回归：合成案例 `PASS`，整体 `PENDING`（真实案例未登记所致，如实记录）。
- 发布一致性检查与发布清单在 `--allow-no-real` 开发模式下达到 `conditional`，
  不代表生产发布门槛已关闭。

### 仍是 PENDING / NOT VERIFIED（不得误读为已通过）

- 真实工程扫描 PDF（中文、表格、印章、旋转页、低质量复印件）的 OCR 质量回归；
  可公开入库的脱敏黄金样本登记仍为 0。
- OCR 合同候选的人工确认界面与 confirmed 事实生命周期闭环。
- 真实 Microsoft Excel、WPS、macOS Excel 的真机打开/关闭/重算/保存/复开核对；
  Windows Excel、WPS、macOS Excel 四种 Office 环境专项仍分别待验证。
- 1万、5万、20万行完整导出基准的同环境 before/after 实测；本版未修改
  `excel_export.py`，不给出任何性能改善数字。
- Windows 代码签名与 macOS 公证；签名与公证仍是发布门槛。

## 安装与兼容

源码运行仍为 Python 3.12 + `uv sync`。macOS Apple Silicon 继续作为主平台；
Windows x64 安装包与便携包为未签名构建物，下载前请核对同一 Release 的
SHA256SUMS.txt。不要将 `PENDING / NOT VERIFIED` 误读为已通过的真机能力，
也不要把预览候选当成交付生产结论使用。
