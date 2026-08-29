# ADR-001: Python 3.12 + uv 管理环境
状态: Accepted | 日期: 2026-08-29
## 背景
需要 macOS arm64 与 Windows x64 双平台长期可用的运行时，且打包为普通用户可安装的桌面软件。
## 决策
- Python 3.12（两大平台成熟 wheel 覆盖最全，PySide6/PyInstaller 支持稳定）。
- uv 管理虚拟环境与锁文件；不依赖系统 Python。
## 理由
- 3.13/3.14 较新，PySide6/PyInstaller 兼容风险高；3.12 为当前桌面打包甜点版本。
- uv 可复现锁版本，CI 与本机一致。
## 后果
打包体积大于原生方案；通过 PyInstaller 裁剪缓解。新版本 Python 升级需 ADR。
