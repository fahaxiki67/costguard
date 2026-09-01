# Jiadun（价盾）v0.1.13 — Windows 测试兼容与 demo 确定性接续预发行

## 本版定位

v0.1.13 是 v0.1.12 实际使用入口版本之上的接续预发行版和预览候选。本版不改变
任何解析、计算或校核行为，聚焦两件工程事项：让符号链接相关测试在无特权的
Windows 环境可靠跳过而非报错，以及让 demo 数据生成在 Windows 与 macOS 产出
字节一致的结果。本版仍为 Preview/Prerelease，只适合合成或已经授权脱敏资料的
试用、规则验证和流程反馈，不代表正式结算、付款、责任、审批或经营结论。

## 主要变化

### Windows 测试兼容（符号链接守卫）

- tests/unit/test_project_and_files.py、tests/unit/test_run_contract.py、
  tests/unit/test_ui_import_flow.py 中直接创建符号链接的测试，在环境无符号
  链接特权（Windows 未开启开发者模式/非管理员，WinError 1314）时改为
  `pytest.skip`，不再表现为测试失败；有特权环境与 macOS CI 行为不变。
- 涉及场景：工作空间符号链接去重、存储副本符号链接循环降级为 unavailable、
  资料导入扫描拒绝符号链接文件/目录/根目录。

### demo 数据生成跨平台确定性

- 生成的 zip 条目 `create_system` 固定为 3（Unix），Windows 与 macOS 生成的
  demo 压缩包字节一致。
- 杀毒软件可能短暂锁定刚写完的文件导致 `os.replace` 抛 WinError 5；现在按
  退避间隔重试（至多 6 次），消除 demo 生成的偶发失败。
- 两处均为早期 Windows 现场反馈（issue #9）的根因修复。

## 升级与兼容

- 无数据库结构变更、无配置变更；从 v0.1.12 直接覆盖安装即可。
- 项目数据库、原始文件与历史导出不受影响。

## 验证记录与发布边界

- 本地自动化回归：全量 pytest 通过（Windows 实测 rc=0，4 skipped，其中 3 项为
  符号链接守卫）；`release_consistency_check --json` ok=true，合成黄金回归
  status=passed。
- 合成黄金回归可用于验证执行器；真实脱敏黄金案例当前仍为 0，不能替代真实
  项目验收。
- WPS macOS、WPS Windows、macOS Excel、Windows Excel 的打开/重算/中文字体/
  筛选/打印/Evidence 跳转仍需现场记录。
- 1万、5万、20万行导入、搜索、异常、匹配、校核、Excel 导出、进度、取消和
  半成品清理仍需现场记录；未执行的规模门禁不得写成通过。
- Developer ID 签名、Apple 公证、跨系统安装与异常恢复仍未完成，当前构建为
  macOS Apple Silicon ad-hoc 本地签名预览版。

关闭以上生产门槛前，v0.1.13 不能标记为正式生产版。

## 安装和兼容

macOS Apple Silicon 预发行资产：

`Jiadun-0.1.13-macos-arm64.dmg`

下载后使用同一 Release 中的 `SHA256SUMS.txt` 校验。旧 `costguard` 图形命令、
GitHub 仓库地址、历史 tag、旧项目目录和旧导出文件保留兼容意义；升级不会删除、
覆盖或移动原始工程资料。
