# 任务 B 批次报告：Sheet 选择与角色确认·服务层（2026-09-05 夜间）

## 1. 本批目标

任务书 §十七 第二阶段任务 B（Sheet 选择和角色确认，P0），服务层全部 +
解析器捕获 + 合同失效联动 + 测试；UI 对话框另行批次。前置：v0.1.26 已于
09-05 早上发布（四资产齐全，标签 84906d5）。

## 2. 实际修改文件

| 文件 | 变化 |
| --- | --- |
| `src/jiadun/core/db/migrations.py` | 新增迁移 v54：`raw_sheets.visible_state`（工作簿级可见状态） |
| `src/jiadun/core/parsing/excel_parser.py` | SheetRecord 新增 `visible_state`；openpyxl 路径取 `ws.sheet_state`，xlrd 路径取 `book.sheet_visibility`；落库写入该列 |
| `src/jiadun/core/engine/sheet_digest.py` | **新增**：Sheet 单元格摘要唯一实现（缓存感知），结转判定与 Run Contract 共用 |
| `src/jiadun/core/engine/sheet_inventory.py` | B1：清单返回 visible_state、置信度（高/中/低）、表头特征提示；B2：filter_mode（all/pending/suggested）；B3：枚举扩展 upstream_detail/downstream_detail/other_fee；B4：`carry_forward_sheet_decisions`；B5：set_sheet_list_kind Evidence 增补（来源文件+哈希+机器建议+置信度+状态） |
| `src/jiadun/core/contracts/run_contract.py` | `_sheet_scope` 纳入 `list_kind`；摘要计算委托 sheet_digest；`CONTRACT_FORMAT_VERSION` 1→2 |
| `src/jiadun/core/engine/settlement_io.py` | 导入循环结束后调用结转；ImportReport 新增 `carry_forward` 统计字段；结转后按库内实际 pending 重算 has_pending，报告不虚报 |
| `tests/unit/test_sheet_inventory.py` | 新增 6 组测试类（见 §5） |
| `tests/golden/cases.json` | run_contract.schema_version 53→54 |
| `docs/GOLDEN_BASELINE_CHANGELOG_v0.1.27.md` | **新增**：基线变更原因与复算方式 |
| `docs/user_feedback/2026-09-04_首次实测反馈.md` | #2/#5 状态更新 |

## 3. 业务语义变化

- **无金额语义变化。** 不改变任何计算、门控判定或金额口径。
- 状态语义变化一：Sheet 清单类型（list_kind）变更现在使 Run Contract
  签名变化，旧运行自动失效（任务书 B5 的硬性要求）。
- 状态语义变化二：重解析（同文件再次导入）后，与上一批次**同名且单元格
  内容摘要一致**的 Sheet 自动沿用原人工确认（sheet_status 与 list_kind）；
  内容变化或无同名旧页的一律保持待确认。人工确认优先于机器门控。
- 展示语义：清单可报告可见状态；历史批次该值为 NULL，展示层必须作
  「未知」，不得默认可见。

## 4. 数据库变化

- migration v54（三项）：
  1. `ALTER TABLE raw_sheets ADD COLUMN visible_state TEXT`（允许 NULL，
     向后兼容；旧项目打开自动迁移，迁移前自动备份）；
  2-4. **摘要缓存失效触发器**（insert/update/delete on raw_cells → 删除
     对应 sheet_id 的缓存摘要）。这是本批关键安全补丁：排查中发现 v53
     缓存表此前**无任何写入方**（真正起效的是 84906d5 的进程内记忆化），
     而任务 B 的结转功能需要持久摘要；一旦摘要被持久化而 raw_cells 被绕过
     触发器改写（旧库/外部 SQL 场景），陈旧摘要会让范围快照漂移闸门失明
     ——`test_verification_rejects_raw_cell_content_drift` 首次全量回归
     即抓到此问题。补失效触发器后，任何 raw_cells 写入都使对应缓存失效，
     缓存化与漂移检测两者兼容（该测试已回绿）。
- 旧批次 visible_state 保持 NULL＝未知（fail-closed，不回填猜测值）。
- 回滚：v54 只加列与触发器，回退到旧版本代码即可忽略；备份在项目
  backups 目录。
- 附带：`sheet_cell_digests` 缓存表现由 engine.sheet_digest 单点读写，
  算法与 v53 定义逐字节一致，清空重建语义不变。

## 5. 测试结果

- 新增测试：`test_sheet_inventory.py` 新增 18 个（枚举扩展/置信度/过滤
  模式/可见状态×3/30 Sheet/结转×3/合同失效/摘要共享实现），连同原有
  6 个共 24 个全过。
- 相关测试：`test_sheet_cell_digest_cache.py` 3 个回归全过（摘要算法
  收敛到单一实现后行为不变）。
- 全量测试：pytest 全量以退出码 0 收口（三次跑通：第一次抓到摘要缓存
  漂移失明，第二次抓到触发器重放不幂等，第三次全绿）。
- ruff：通过（All checks passed）。
- Golden：`scripts/golden_regression.py --json` → status=passed；
  demo_synthetic_v1 PASS，sanitized_real_template PENDING（无真值，符合预期）。
- 性能：未验证（本批无性能样本变更）。
- Office/WPS 真机：未验证。
- OCR：未验证。

## 6. 真实资料结果

本批未新导入真实资料。既有的语料库盘点（`local_private_data/
corpus_inventory/`）仍只有元数据清单，隔离副本层未建（任务 A 的下一块）。
无 PASS/FAIL 结论可报；不因无真值而宣称业务计算正确。

## 7. 已知风险

- 结转按「同名 + 摘要一致」判定：同名不同义的 Sheet（内容恰好逐格一致）
  会结转——工程实务中同名同内容通常确为同一张表，风险低但非零；
  Evidence 里记录了结转依据，可人工追溯。
- `visible_state` 需重新导入才能获得；历史批次显示「未知」是有意设计。
- UI 尚未接入清单服务（反馈#2 的界面侧），用户可感知的改善要等 UI 批次。
- `carry_forward_sheet_decisions` 在超大工作簿（数百 Sheet）下首次导入
  会多算一遍摘要（同时也是缓存预热，后续 refresh 受益）；50 张结转以上
  Evidence 只记前 50 条明细并注明截断。
- 摘要缓存失效触发器只覆盖 raw_cells 的 INSERT/UPDATE/DELETE；DROP
  TRIGGER 后再改内容（本批漂移测试的构造）仍可绕过——但那与既有
  不可变触发器同一威胁等级，读取闸门在触发器在位时全程有效。

## 8. 回滚方式

- 本批提交：见 git log（单批多文件提交，标注 golden 54）。
- migration 影响：v54 仅加列；回退代码即可，无需回滚数据库。
- 数据恢复：迁移前自动备份（项目 backups 目录）；缓存表可随时清空。

## 9. 下一批建议

1. UI 批次：Sheet 清单浏览器对话框（接入 list_workbook_sheets 全部能力
   ：过滤模式/置信度/可见状态/下拉改注），直接闭环用户反馈#2/#5 的界面侧。
2. 任务 A 补课：Golden 候选 22 项的隔离副本 + SHA-256 前后核验 + 逐文件
   导入验证（语料库目前只有元数据盘点）。
3. 任务 C 税口径（反馈#4，P0）：独立税金列识别 + 口径 Evidence + 人工改判。
