# CostGuard v0.1.2 — macOS DMG 预览（未公证）

面向 macOS Apple Silicon 普通用户的第一个可双击安装的预览版。

## 本次新增

- **CostGuard-0.1.2-macos-arm64.dmg**：双击挂载 → 把 CostGuard.app 拖入
  Applications → 启动。**不需要安装 Python、uv 或任何开发环境。**
  - DMG 内含：CostGuard.app、Applications 快捷方式、
    「三分钟上手（先读我）」说明、「匿名演示数据」文件夹。
  - 最低系统：macOS 15.0（随打包的 PySide6 6.11 二进制实测确定）。
  - arm64 原生构建（`lipo` 校验），不混通用二进制。
- **一键体验匿名演示**：项目列表页点击「体验匿名演示」，自动创建演示项目并
  导入完全合成的演示数据（对上/对下各 3 期 + 合成合同），配合
  [三分钟上手](QUICKSTART_zh-CN.md)走完
  导入 → 标准化 → Decimal 金额校核 → 匹配 → 对上对下差异 → 异常 → 导出 全流程。
- **匿名演示数据**：程序生成（`examples/demo` + `scripts/generate_demo_data.py`），
  固定种子、字节可重复，manifest 记录 SHA-256/方向/期次/预期行数/预期异常/
  已知限制与 28 项场景覆盖矩阵；不含任何真实公司、项目、人员、金额或业务结构。
- **真实界面截图**：`examples/screenshots/`（由 `scripts/generate_screenshots.py`
  驱动真实运行的程序渲染，可本地复现）。
- **打包隐私门槛**：构建流水线内置 `scripts/audit_bundle_privacy.py`——扫描
  安装包未压缩层与解压后的全部 Python 字节码，命中构建机用户名/HOME/仓库
  绝对路径/局域网网段/local_private_data 即构建失败。
- 手动打包工作流（`package-macos-arm64.yml`）：lint → 全量测试 → 演示数据
  确定性 → 构建 → 敏感扫描 → artifact 上传；不自动创建 Release。

## 签名与公证边界（必须如实告知）

- 当前 DMG 为 **ad-hoc 本地签名**，**未经 Apple 公证**。
- 首次启动时 macOS 可能提示"无法验证开发者"：在"应用程序"文件夹
  **右键 CostGuard → 打开 → 打开**，只需一次。不同系统设置下提示形态不同，
  **不能承诺完全不出现 Gatekeeper 提示**。
- Developer ID 签名与公证需要 Apple 开发者账号与证书，属后续版本工作；
  签名材料只从本机钥匙串或 GitHub Secrets 读取，绝不入库。

## 验证状态

- 本地全量测试：226 passed；ruff 全绿。
- DMG 构建流水线全门槛通过：演示数据确定性校验、arm64 架构校验、
  codesign 自检、DMG 挂载自检、隐私审计 PASS。
- 打包产物实机验证：从 dist 目录经 LaunchServices 启动（无源码、无虚拟环境）、
  常驻事件循环、干净退出、不留副作用目录。
- 演示数据确定性：独立进程重新生成与仓库内文件字节一致（`--check` 门槛）。
- 自动计算结果是技术校核结果，必须经人工复核与业务审批，不构成已批准业务结论。

## 升级与数据安全

- 项目资料保存在用户本机 `~/Documents/CostGuardProjects/`，与软件分离；
  覆盖安装/删除 App 不影响项目资料。
- 软件不做后台静默更新；新版本提示由用户自行决定。

## 已知限制

- 未公证（见上）；Windows 版本在路线图 Phase 15–16（共享同一核心引擎）。
- 旧版 `.doc`、扫描件 OCR 未实现；扫描资料不得直接形成业务结论。
- 汇总/台账类表格会被角色门控拦截，需人工确认后才会写入结算模型（设计行为）。
