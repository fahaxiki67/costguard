# Jiadun（价盾）v0.1.19 — 备份恢复与 Windows 兼容预发行

## 本版定位

v0.1.19 是在 v0.1.18 基础上的安全加固预览候选，重点处理备份恢复边界、
Windows Excel/WPS 工作簿所有权和大规模 Excel 导出性能。长期平台战略保持不变：
macOS Apple Silicon 仍是 P0 主平台，Windows x64 仍是 P1，共享业务核心保持跨平台，
Windows 特有逻辑继续放在 `platform/` 层。本版不标记为正式生产版。

## 主要变化

### 备份恢复安全（P0）

- 恢复前完整校验 ZIP 与 `manifest.json`：拒绝 `../`、`..\\`、绝对路径、Windows
  盘符、UNC、空路径段、非法 Windows 名称和保留设备名。
- 拒绝 ZIP 重复文件名、大小写/Unicode 碰撞、manifest 重复路径、重复 JSON 键、
  manifest 与 ZIP 文件集合/大小不一致，以及 root `manifest.json` 冲突。
- 拒绝 ZIP 目录、symlink 和特殊文件；读取与解压阶段检查 symlink、junction、
  Windows reparse point 和路径越界，发现异常直接 fail-closed，不清洗后继续。
- 恢复先在目标父目录内的随机 staging 中完成哈希、SQLite `integrity_check` 和
  打开验证；已有同名项目目录（包括空目录）拒绝覆盖。提交阶段使用
  `platform/secure_fs.py` 的目录句柄相对独占创建（POSIX `dir_fd`/`O_NOFOLLOW`，
  Windows `NtCreateFile(RootDirectory=...)`），失败清理只针对本次创建且身份/内容
  仍匹配的普通文件和空目录。

### Windows Excel/WPS 工作簿所有权（P0）

- 跳转前记录 Workbook 快照，并区分 `workbook_owned_by_jiadun=True/False`；调用
  前已打开的目标 Workbook 直接复用，不再次打开、不关闭。
- 只有 COM 身份能够证明为本次 `Open` 新建的 Workbook 才允许
  `Close(SaveChanges=False)`；无法确认时保守不关闭。
- 已存在的 Excel/WPS 实例不 `Quit`；只有 `DispatchEx` 明确创建、进程快照足够且
  退出前确认没有 Workbook 的实例才允许 `Quit`。
- 保留无 COM 时的只打开降级路径。真实 Microsoft Excel 真机没有在本机验证，
  因此相关结论为 `PENDING / NOT VERIFIED`，mock/WPS 结果不写成 Excel PASS。

### Excel 导出性能（P1）

- `_autowidth()` 改为一次行主序扫描；Evidence 列表只建立一次索引，Evidence
  列按需遍历，同时保留非 Evidence 列中 `Evidence ID N` 文本的旧回溯语义。
- `_style_used_range()` 复用已注册的不可变样式 ID，避免每个单元格重复构造相同
  Border/Alignment；没有改变 Decimal、公式、Evidence、待补资料、不可比或审计列。
- 在同一 Windows 环境记录数据准备、Sheet 写入、样式、自动列宽、Evidence、save、
  总耗时和内存；数据库跟踪未发现需要在本轮引入的逐行 N+1 查询，未做无关重构。

## 验证结果与边界

### 10k 同环境 benchmark

正式 benchmark 的 before/after：

| 指标 | before | after | after/before | 变化 |
|---|---:|---:|---:|---:|
| Excel 审核底稿导出 | 1013.171s | 662.354s | 1.530x | -34.626% |
| 完整 benchmark 阶段合计 | 1309.928s | 970.937s | 1.349x | -25.879% |
| Python 导出峰值 | 304.407MB | 304.413MB | 1.000x | +0.006MB |

导出 profiling 阶段（用于定位瓶颈，测量开销与正式 benchmark 不同）：数据准备
293.735s → 296.296s；Sheet 写入 143.693s → 143.670s；样式 63.466s → 63.520s；
自动列宽 1.558s → 2.620s；Evidence hyperlink 370.618s → 1.866s；save
52.126s → 51.994s；profiling 总耗时 989.872s → 623.285s。after profiling 的
Python 峰值约 290.209MB，Windows 工作集采样峰值约 717.551MB；before profiling
的 Windows 工作集指标未可靠采集，不能做该指标的倍数结论。

### 50k 同环境 benchmark

- v0.1.18 before：运行约 3:00:37，在完整 Excel 导出 JSON 前未完成；观测到的工作集
  峰值约 2869.3MB，状态 `TIMEOUT / INCOMPLETE`。
- 当前 after：运行约 1:02:30，在完整 JSON 前为节省开发机资源安全停止；期间 CPU
  持续活动，工作集采样最高约 526.5MB，状态 `TIMEOUT / INCOMPLETE`。
- 因两次都没有完整结果，50k 不给出改善倍数，也不宣称 50k 导出已通过。50k
  完整导出仍是后续发布门槛。

### Windows CI 与真实项目资料

- `.github/workflows/ci.yml` 增加 Windows x64 普通回归：Ruff、完整 pytest、golden
  regression；覆盖中文/空格路径、路径分隔符、文件占用和临时文件相关测试。
- 本机真实资料只读扫描后选择 5 个匿名副本（`.xlsx` 与 `.xls`），最大副本
  4,924,766 bytes；包含隐藏 Sheet、公式、合并单元格、多 Sheet 等结构。全部在
  中文/空格隔离目录中运行。5 个导入结果均为核心定义的 `PENDING`（解析能力边界
  或需人工确认），没有强行匹配、补 0、分析或导出。
- 5 个原始文件测试前后 SHA-256、文件大小、修改时间全部不变；测试副本、数据库、
  导出/临时目录均在仓库外。真实 Microsoft Excel、WPS、macOS Excel 打开关闭、
  权限异常和 reparse point 真机验证仍为 `PENDING / NOT VERIFIED`；1万、5万、
  20万行完整大规模基准也仍是后续门槛。

## 保留的原则与未解决风险

- Decimal 确定性计算、原始文件只读、Evidence 可追溯、Run Contract、缺失不补 0、
  不可比不强行匹配和 fail-closed 均保留。
- 恢复目标写入已经改为目录句柄相对操作，但源文件遍历/快照的同大小并发改写、
  恢复打开验证与清理阶段的完整并发矩阵、特权级文件系统变更和 COM 进程快照仍
  不能完成内核级身份证明；真实 Excel/WPS/不同 openpyxl 版本尚未完全验证，仍
  需后续专项复核。ZIP 资源上限已存在但仍需进一步评估中心目录、压缩比和超大
  包的更早拒绝策略。
- Windows 代码签名、macOS Developer ID 签名/公证、授权脱敏黄金案例和 50k 完整
  导出基准未完成；本版为预发行/预览候选，不是正式生产版。

## 安装与兼容

源码运行仍为 Python 3.12 + `uv sync`。macOS Apple Silicon 继续作为主平台；
Windows x64 包和任何未来的 macOS/Windows 构建物必须以同一 Release 的校验和及
真实平台验证结果为准。下载包前请阅读本说明，不要将 `PENDING / NOT VERIFIED`
误读为已通过的真机能力。
