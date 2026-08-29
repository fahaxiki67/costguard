# ADR-007: 平台抽象层
状态: Accepted | 日期: 2026-08-29
## 决策
全部 OS 差异集中在 src/costguard/platform/（paths、system、packaging）。
core/ 禁止平台分支逻辑；CI 静态检查违规 import。
## 理由
P0=macOS arm64 不被 Windows 需求拖慢，但路径/编码/字体/权限差异必须第一天就隔离，
否则后期重写代价极高。
