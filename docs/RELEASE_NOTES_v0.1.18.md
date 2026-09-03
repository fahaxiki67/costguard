# Jiadun（价盾）v0.1.18 — Windows 生产化与真实工程验证预发行

## 本版定位

v0.1.18 在 v0.1.17 的基础上完成 Windows x64 生产化改造与真实工程案例验证：
真机跑通安装/启动/退出/卸载、路径与 DPI 矩阵、WPS/COM 全链路、数据库破坏性
恢复，以及 14 个真实工程结算文件的人工真值黄金案例。本版仍是预览候选，
不代表正式生产版，也不替代签名公证、真 Microsoft Excel 环境和完整大规模
基准验收。

## 主要变化

### Windows 打包与平台层

- EXE 版本资源（产品名 Jiadun（价盾）/版本/Publisher）与
  PerMonitorV2 DPI、长路径 manifest 在构建期生成并嵌入；构建脚本新增
  可选 signtool 签名钩子（`JIADUN_SIGN_PFX`/`JIADUN_SIGN_PFX_PASSWORD`）
  与版本资源校验步骤；正式发布版必须有数字签名，本版仍未签名，如实提示。
- 窗口图标在 Windows 正确使用 `.ico`（此前仅识别 `.icns` 导致窗口无图标）。
- 应用入口显式 DPI 取整策略（PassThrough），界面字体继续按平台选择
  Microsoft YaHei UI 等系统字体，不在业务计算中引入浮点数。
- 工作台新增 Windows 键盘习惯：Ctrl+F 搜索、Ctrl+A 全选、Ctrl+C 复制选中、
  Esc 清空/取消选择；只读审计表格不绑定任何删除类快捷键。

### COM 纪律修复（WPS / Excel）

- 跳转打开源文件（`platform/spreadsheet_jump.py`）重写实例归属判定：
  先 `GetActiveObject` 附着已开实例，再以进程快照（PID 集合）判定是否为
  本次全新启动；只有"调用前无任何表格进程且出现新 PID"才允许 Quit，
  其余情况只关闭自己打开的工作簿。
- 全程不调用 `CoUninitialize`：实测中途调用会拆掉 GUI 线程 STA 上所有
  存活 COM 代理（用户会话断连）；该行为已用守护实例实验复现并修复。
- 真机四模式验证（强制 Excel ProgID / 强制 WPS Ket / 回退链 / 无 COM
  降级）全部通过：定位成功、守护工作簿存活、无进程残留。
- 导出→WPS 打开→重算→保存→复开→与程序 Decimal 结果逐格核对：0 漂移，
  审核底稿公式复核值与程序值一致。

### 数据可信与恢复

- 新增项目备份/恢复/完整性检查（`core/backup_restore.py`）：SQLite
  backup API 一致性快照、清单 SHA-256 校验、恢复防覆盖与防篡改；
  数据库升级前自动备份机制（v0.1.17 已有）继续保留。
- 破坏性测试 7 场景全部通过：导入/分析/导出过程中强杀进程、外部进程
  锁库、项目目录整体移动（originals 路径自动修复）、OneDrive 同步冲突
  副本、v0.1.16 旧库升级（schema 45 迁移、数据零丢失、迁移前备份留存）。
- 经营合规问题台账（migrations v46 + `core/ledger.py`）：金额影响/
  责任单位/责任事项/处理意见/状态只能由人工写入并留审计事件，按
  fingerprint 跨运行定位；异常清单导出追加台账列。存在差异不等于存在
  违规，事实差异与风险判断继续分离。

### 真实工程黄金案例（仅本地，不入库）

- 登记真实工程结算语料 14 文件（对下分包多期结算、结算台账、专项调差、
  旧版 xls），存于 `local_private_data/`，公开仓库不收录未脱敏资料。
- 每期 A 路合计与人工逐列核对的真值在 Decimal 精度下相等；累计金额等于
  各期金额之和；跨文档累计链与跨期匹配通过；6 类破坏性变体（删行、
  复制行、改金额、改数量、改单价、隐藏行）全部被检出。
- 真实文件中检出的重复项、公式错误、合并单元格数据、小计不符等高风险
  发现与源表一致；C 控制值缺失时校核保持"不充分"，不以 A/B 相等放行。

## 验证结果与边界

- Windows 10 x64 真机：安装、启动、优雅退出、卸载（用户工程数据不受
  卸载影响）通过；OneDrive 中文路径建项目导入、299 字符长路径、文件
  占用与无写权限的优雅报错通过。
- WPS 表格全程可用：导入、解析确认、校核、匹配、导出、COM 定位与重算
  回环均在本机 WPS 环境完成。Windows Excel、macOS Excel/WPS 专项仍未在
  真机复核。
- 性能：1 万行完整八阶段基准通过；5 万行除 Excel 审核底稿导出外通过
  （导入 1474s、双向校核 806s）；**审核底稿导出在大规模行数下呈超线性
  （5 万行 >2 小时未完成），20 万行完整基准尚未执行**，导出性能优化是
  下一版优先事项。长任务取消语义保持 fail-closed，取消状态只标记为
  条件状态。
- 全量 `pytest` 与 Ruff 通过（含黄金回归、发布一致性、迁移、备份恢复、
  台账、COM 注入测试）。`release_consistency_check.py --allow-no-real`
  通过；登记真实黄金案例数为 0（真实语料仅存本地），生产发布门禁仍为
  `production_release_ready=false`。
- Windows 代码签名未完成（无证书，构建钩子已就绪）；Apple Developer ID
  签名与公证同样未完成。**在 WPS/Windows Excel/macOS Excel 真机专项、
  1万/5万/20万行完整基准、签名与公证、真实案例独立复核关闭以上生产门槛
  前 v0.1.18 不能标记为正式生产版。**

## 安装与数据安全

- Windows x64：`Jiadun-0.1.18-windows-x64-setup.exe`（按用户安装，无需
  管理员）或 `Jiadun-0.1.18-windows-x64-portable.zip` 免安装版。安装包
  未签名，SmartScreen 可能提示；卸载只清除程序目录，永不触碰
  `Documents\JiadunProjects` 用户工程数据。
- macOS Apple Silicon：由 CI 构建的 `Jiadun-0.1.18-macos-arm64.dmg`
  （ad-hoc 本地签名，未公证）。
- 源码运行：Python 3.12 + `uv sync`，`uv run jiadun`。
- 原始文件只读；缺失资料显示"待补资料"不补 0；不可比项目不强行匹配；
  AI 不参与确定性金额计算。下载后使用同一 Release 的 `SHA256SUMS.txt`
  校验完整性；校验和不等于签名或公证证明。
