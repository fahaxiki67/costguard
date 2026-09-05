# 结论性框架闭环（接力二轮）阶段报告（2026-09-05）

## 任务来源

用户接力任务书（接力二轮）三项主攻：
1. 前序真实结算实测报告问题消化——前序实测止步于导入冒烟（§三~§十一），
   深度对比 §十二~§二十五 被 F-3 阻塞（F-3 已在前序 v0.1.26 修复）；
   **尚无对上/对下比较实测报告**，故无可消化的 needs_review/INCOMPARABLE 明细，
   本轮改为从代码实测中发现并修复问题（见下）。
2. 框架/管理性协议独立算法：v51 已有候选→确认生命周期；本轮补基数自动
   解析（任务 2）。
3. 对上/对下结论性框架接问题中心与报告导出（任务 3）。

## 本轮发现并修复的问题

### Bug 1（红→绿）：税口径词表不一致 → 同义口径误判 INCOMPARABLE
- 基准/费率对话框与确认入口使用 `included/excluded`，
  期次 `tax_mode` 使用 `incl_tax/excl_tax`——`compare_upward_result`
  直接字符串比较，同义口径（基准 included vs 期次 incl_tax）恒判
  INCOMPARABLE，且期次税口径无人工确认入口导致比较恒 PENDING。
- 修复：canonical 词表 `incl_tax/excl_tax/unknown`；比较两侧读侧归一；
  登记确认写侧归一；新增 `set_period_tax_mode` 人工确认（依据必填+
  Evidence）。回归：`TestTaxVocabularyNormalization`（旧代码必红）。

### Bug 2：并行会话在制 `_DAYS` 改动引入崩溃（IndexError: no such group）
- 并行会话把 `_DAYS` 单位组改为非捕获但保留 `m2.group(2)` 消费者，
  任何含时限条款（工期 X 天/28 天内）的合同导入即崩，主测试文件全挂。
- 修复（最小、恢复 HEAD 语义）：恢复单位捕获组；未触碰并行会话其余
  在制内容（大写金额解析、party guard、PDF 页复核改进等）。

### 费率基数自动关联（任务 2 实现）
`resolve_rate_base` + `apply_rate_rule_to_settlement`（独立于实体工程量
清单算法）：upward/downward 期次合计、唯一已确认合同价款事实、custom
人工基数；税口径未确认→pending、混用/不一致→incomparable、金额缺失行
→pending（缺失不按 0 参与合计）、多条已确认金额事实→conflict 不自动挑选、
空值占位事实显式排除不参与、不含税基数禁止从含税合计按猜测税率换算；
被阻断的试算尝试也落档（rate_rule_applications + Evidence）。

### 结论持久化与报告导出（任务 3 实现）
- 迁移 v54：`control_conclusions` / `rate_rule_applications`（只追加）。
- Excel 新增「对上控制基准结论」「费率规则与试算」两表；新模块
  `export/conclusions_report.py` 生成 Markdown 结论报告并登记为
  `conclusions_markdown` 受控成果；无结论明示"无结论不等于通过"，
  INCOMPARABLE/CONTROL_CONFLICT 如实呈现，不强行 PASS。
- 问题中心汇总行显示结论计数（非 PASS 计为待关注）；成果导出 tab
  新增结论报告卡片并纳入「全部生成」。

## 验证

- 新增 `tests/unit/test_conclusions_framework.py` 33 项全绿；
  test_control_baseline / test_rate_rules / test_contract_extract /
  test_ui_*（28 项）全绿；ruff 全绿。
- 黄金基线 schema 53→54（附 GOLDEN_BASELINE_CHANGELOG 记录）。
- 全量套件当前 8 项失败，**全部归因并行会话未提交的 extract.py
  party_guard 行为变化**（demo 事实数 8<10、黄金 contract_fact 13→8
  及其级联 release/golden 门槛测试），不属本轮变更，未代为更新基线
  （宪章：禁止为过测试自动更新 golden expected）。

## 并行会话共存声明

本轮会话与另一 ZCode 会话共享工作树；对方在制未提交文件
（extract.py、test_contract_extract.py、scripts/market_probe.py、
tests/unit/test_pdf_review_reimport.py）一律未动、未提交。本轮 `_DAYS`
修复发生在对方文件内，留在工作树供双方共享，随对方提交收口。
