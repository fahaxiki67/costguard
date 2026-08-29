# ADR-003: SQLite 单文件项目库 + 用户可见工作空间
状态: Accepted | 日期: 2026-08-29
## 决策
每个工程项目一个目录：project.db(SQLite) + originals/ + exports/ + backups/。
软件配置存系统配置目录，工程数据存用户自选工作空间。
## 理由
- 数据量级（数万~数十万行明细）SQLite 足够；单文件易备份、易迁移、易被用户理解。
- 目录结构用户可见可备份，符合"软件与数据分离"。
## 后果
并发写入受限（单机单实例，可接受）；超大项目引入只读分析缓存（如 DuckDB）需新 ADR。
## 迁移纪律
schema_version 管理；Migration 前自动备份到 backups/；失败回滚。
