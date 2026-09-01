# Jiadun（价盾）v0.1.15 — 首个 Windows x64 交付版预发行

## 本版定位

v0.1.15 是 v0.1.14 结算可信度闸门版本之上的接续预发行版和预览候选，**首次提供
Windows x64 安装包与便携包**，并把 Windows 交付流水线整体入库。本版不改变任何
解析、计算或校核行为。本版仍为 Preview/Prerelease，只适合合成或已经授权脱敏
资料的试用、规则验证和流程反馈，不代表正式结算、付款、责任、审批或经营结论。

## 主要变化

### Windows x64 交付流水线（入库）

- PyInstaller onedir 构建（GUI，无控制台窗口），主程序 `Jiadun.exe`；
- Inno Setup 6 按用户安装器（`PrivilegesRequired=lowest`，无需管理员；仅安装
  程序本体，用户工程数据 `Documents\JiadunProjects` 永不触碰；无后台常驻、
  无自动更新、无驱动）；
- 便携版 zip（解压即用，不写注册表）；
- 交付门槛脚本化：演示数据确定性校验 → PE 架构校验（必须 x64）→ 安装包隐私
  审计（本机身份/私有信息零嵌入；新增 Windows HOME 反斜杠形态扫描）→
  安装/启动/退出/卸载真机冒烟 → SHA256 汇总；
- 新增手动触发的 Windows x64 CI 流水线（真实 Windows runner 全门槛）与
  Windows 移植验收测试（tests/unit/test_windows_port.py）。

### 修复（Windows 链路）

- 安装器脚本的相对路径错误（该流水线此前从未在本地端到端跑通过）；
- 隐私审计此前只识别 macOS bundle 布局，现同时接受 Windows onedir 布局；
- 冒烟端到端适配 v0.1.10+ 的"period_totals 聚合回写"校核闸门。

## 升级与兼容

- 无数据库结构变更、无配置变更；从 v0.1.13/v0.1.14 直接覆盖安装即可。
- 项目数据库、原始文件与历史导出不受影响；卸载只删除程序目录，用户数据
  `Documents\JiadunProjects` 永不清除。

## 安全提示

Windows 安装包与便携包**均未签名**：SmartScreen 可能提示"未知发布者"，部分
杀毒软件可能对 PyInstaller 产物误报；请通过 Release 页的 SHA256SUMS.txt 核对
哈希后使用。文档如实说明，不承诺无提示。

## 验证记录与发布边界

- 本地自动化回归：全量 pytest 通过（Windows 实测 rc=0，4 skipped，其中 3 项为
  符号链接守卫）；`release_consistency_check --json --allow-no-real` ok=true，
  合成黄金回归 status=passed；演示数据确定性校验通过；PE 架构校验 x64 通过；
  隐私审计通过；安装/启动/退出/卸载冒烟通过。
- 合成黄金回归可用于验证执行器；真实脱敏黄金案例当前仍为 0（本次检查以
  `--allow-no-real` 有条件通过），不能替代真实项目验收。
- WPS macOS、WPS Windows、macOS Excel、Windows Excel 的打开/重算/中文字体/
  筛选/打印/Evidence 跳转仍需现场记录。
- 1万、5万、20万行导入、搜索、异常、匹配、校核、Excel 导出、进度、取消和
  半成品清理仍需现场记录；未执行的规模门禁不得写成通过。
- Developer ID 签名、Apple 公证、Windows 代码签名、跨系统安装与异常恢复仍未
  完成，当前构建为 macOS ad-hoc 签名 DMG 与未签名 Windows 包的预览版。

关闭以上生产门槛前，v0.1.15 不能标记为正式生产版。

## 安装和兼容

- macOS Apple Silicon：`Jiadun-0.1.15-macos-arm64.dmg`
- Windows x64 安装版：`Jiadun-0.1.15-windows-x64-setup.exe`（按用户安装，双击
  即可，无需管理员）
- Windows x64 便携版：`Jiadun-0.1.15-windows-x64-portable.zip`（解压即用）

下载后使用同一 Release 中的 `SHA256SUMS.txt` 校验。旧 `costguard` 图形命令、
GitHub 仓库地址、历史 tag、旧项目目录和旧导出文件保留兼容意义；升级不会删除、
覆盖或移动原始工程资料。
