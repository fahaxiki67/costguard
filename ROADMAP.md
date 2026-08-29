# ROADMAP.md — CostGuard 开发路线图

> 平台顺序固定：P0 macOS Apple Silicon → P1 Windows x64 → P2 待定。
> 每个 Phase 完成：设计 → 实现 → 单元测试 → 集成测试 → Review → 修复 → 回归 → 文档。

## 状态图例
✅ 完成 | 🔨 进行中 | ⬜ 未开始

| Phase | 内容 | 状态 |
|-------|------|------|
| 0 | 总体架构与跨平台设计：目录骨架、ADR、数据模型、风险自检 | ✅ |
| 1 | macOS ARM64 最小可运行框架：平台层、DB+迁移、项目管理、PySide6 壳 | ✅ |
| 2 | Excel 导入和结构化：解析保真层、表头识别、清洗、合成测试数据 | ✅ |
| 3 | 结算计算引擎：期次累计、加权平均单价、双向校核（含一簿多期迁移v2） | ✅ |
| 4 | 异常检测：23+ 规则引擎（21条规则，含舍入差异分级） | ✅ |
| 5 | 证据链：Evidence ID、审计日志、匹配五档置信度 | 🔨 |
| 6 | 合同和合规分析 | ⬜ |
| 7 | 成果导出：12 类报表、Excel 保留公式、WPS 兼容 | ⬜ |
| 8 | 完整 Mac UI | ⬜ |
| 9 | Mac 大规模测试和性能优化 | ⬜ |
| 10 | Mac 打包（unsigned DMG） | ⬜ |
| 11 | GitHub CI/CD | ⬜ |
| 12 | Mac Beta Release (v0.9.x) | ⬜ |
| 13 | 真实案例回归测试和 Bug 修复 | ⬜ |
| 14 | Mac V1.0 Release Candidate（签名+公证预留） | ⬜ |
| 15 | Windows x64 兼容 | ⬜ |
| 16 | Windows 安装包 | ⬜ |
| 17 | Windows Beta | ⬜ |
| 18 | 跨平台 V1.x | ⬜ |

## Phase 0 退出标准
- [x] 目录骨架与 Git 仓库建立，.gitignore 隔离 `local_private_data/`
- [x] AGENTS.md 固化最高优先级原则
- [x] ARCHITECTURE.md（含数据模型、证据链设计、风险对策）
- [x] ADR-001..010
- [x] ROADMAP / README 双语 / 社区文件 / CHANGELOG
- [ ] Python 3.12 环境 + 关键依赖导入验证
- [ ] Phase 0 commit

## 自动升级策略（Phase 12 起生效）
提示新版本 → 用户点击查看 GitHub Release → 用户主动决定升级。
不实现后台静默更新。
