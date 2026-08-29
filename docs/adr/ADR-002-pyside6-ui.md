# ADR-002: PySide6 作为共享桌面 UI
状态: Accepted | 日期: 2026-08-29
## 决策
UI 层采用 PySide6 (LGPL-3.0)，Mac/Windows 共用同一套界面代码。
## 理由
- 官方 Qt 绑定、LGPL 商用友好；macOS arm64 + Windows x64 wheel 齐全。
- 表格/树视图成熟，适合清单核对场景；无需引入 Web 服务架构。
## 后果
打包体积较大；用 PyInstaller exclude 裁剪无用 Qt 模块。禁止在 core 层 import PySide6。
