# v0.1.16 后续 P0 信任加固设计规格

## 1. 目标与范围

本批次只处理“真实项目可验证性”的缺口，不新增业务功能，不改变结算计算口径，不升版本、不创建 Release。目标是让当前主线在黄金回归和私有资料预检不完整时保持 fail-closed，并把 A/B/C 路径的独立性边界固定为可回归证据。

本批次交付范围：

1. 私有真实资料验收执行器遇到副本缺失或哈希不符时，不在预检阶段直接抛出未解释的断言；建立新的 run 目录，逐项记录“待补资料/完整性待复核”，只对完整且哈希一致的副本继续执行。
2. 为预检状态建立机器可读字段和中文报告状态；缺失资料不参与金额、匹配、异常或导出计算，也不得产生绿色“通过”。
3. 补充 A/B/C 双路径同源污染、覆盖范围、C 不可用/未解析行等回归测试，锁定当前 fail-closed 行为。
4. 复核黄金回归执行器保持闭世界比较、禁止自动更新基线、禁止读取 `local_private_data/` 的约束；登记表类型、案例版本、资产目录、真实案例 provenance 和可用案例输入 SHA-256 均强制绑定，演示资产不得改标为真实案例。
5. 收紧发布清单性能证据 schema，防止空结果、重复规模、重复阶段、缺阶段、跳过 Excel 导出、伪造导出路径或哈希被判为通过；内外两条性能路径共用同一严格验证器，验收导出失败时不留下可误拿的部分产物。
6. 对源工作簿增加独立 Sheet 目录/范围盘点：`dimension` 缺失时使用 `row/c` 坐标扫描；解析结果在首个物化 INSERT 前校验 Sheet 索引和网格边界；完整导入由外层事务包住，覆盖证明或后续阶段失败时只保留 failed 批次与 Evidence；完整性能报告的每个阶段也必须是结构完整的对象。

不在本批次范围：GUI 新功能、匹配算法扩展、AI、性能优化、Office 真机验收、签名/公证、真实私有资料补录和 Release 发布。性能只增加证据完整性闸门，不宣称完成性能优化；这些事项在 `CURRENT_STATE_AUDIT.md` 中保持 PENDING。

## 2. 不可变约束

- 原始文件只读；验收执行器不得修改 `local_private_data/` 中的副本、清单或历史结果。
- 金额、数量、单价仍全部使用 `Decimal`；本批次不引入新的金额计算。
- 缺失值不补 0；缺失副本、哈希不符、范围未证明、C 控制值不可用或存在未解析行，统一保留待补资料/证据不足状态。
- A/B 数字相等不等于校核充分。只有运行级路径独立性、覆盖证明、控制值和 Evidence 门槛均满足时，才允许既有“sufficient”状态。
- 黄金基线只读比较；任何金额、行数、匹配、异常或 Evidence 变化必须报告差异，不能自动改写 expected。

## 3. 设计

### 3.1 语料预检

在 `scripts/real_acceptance_run.py` 增加纯函数 `summarize_corpus_preflight(records)`，输入为 `verify_corpus()` 的记录，输出：

- `status`: `ready` 或 `pending`；
- `missing_test_ids`: 副本不存在的 test_id；
- `hash_mismatch_test_ids`: 副本存在但哈希不一致的 test_id；
- `record_count`、`ready_count`、`pending_count`。

`main()` 仍对清单行数不足和重复 `test_id` 快速失败（这是清单契约错误，不是业务资料缺失），但不再对 `bad` 副本使用裸 `assert`。每个预检不通过的 test_id 生成独立 done marker，状态为 `pending_source_data`，并跳过 `inspect_file()`；完整副本照常走现有全链路。哈希读取本身发生权限/占用错误时也归入完整性 pending，并保留可解释错误。

预检 pending 结果至少含：

```json
{
  "preflight": {"status": "pending", "exists": false, "hash_match": false},
  "steps": {
    "technical_execution_complete": false,
    "technical_validation_status": "not_run_or_incomplete",
    "overall_acceptance_status": "pending_source_data",
    "verification_level": "insufficient"
  }
}
```

结果中保留预检期望哈希、实际哈希（缺失时为 `null`）和相对路径；不得把缺失文件伪装为解析失败、金额为零或“无发现”。顶层报告增加 `preflight` 摘要，中文报告增加“待补资料/完整性待复核”说明。人读报告跟随当前 run 保存，不覆盖旧 run；哈希不符与缺失都不参与技术完成数；`hash_check.before_all_match` 和 `after_all_match` 仍如实为 `false`。若处理期间或处理后副本发生变化，已生成的该文件结果必须写回 `pending_source_data` marker，增加 `previous_result_invalidated=true`，并在 `hash_check.invalidated_results` 和中文报告中列出；不得仅记录哈希差异却继续把旧结果计入技术完成数。

### 3.2 A/B/C 信任回归

在 `tests/unit/test_p0_trust_gates.py` 或相关现有测试文件增加命名清晰的回归：

- 改写当前项目 `line_items.amount` 只能影响 A，B 必须继续读取 `raw_cells` 的原始值；A/B 不一致时项目级校核必须为 `insufficient`/有发现。
- 原始明细漏行、重复行、合计行混入、隐藏/筛选行、跨 Sheet、负数调整等覆盖证明缺口，不得得到项目级 sufficient。
- C 控制值缺失、C 来源未解析或存在未解析行时不得得到 sufficient；Evidence 必须说明来源或限制。

测试只验证确定性状态、来源范围和 Evidence 字段，不写入用户文件，不把测试调整值当作业务结论。

### 3.3 版本与黄金回归边界

本批次不升版本、不改变发布契约；将源码工作树的版本读取收敛到
`src/jiadun/version.py`，以 `pyproject.toml` 的 `[project].version` 作为唯一真源，
安装包 metadata 仅作为找不到源码清单时的只读回退。Run Contract、验收报告和发布清单
均通过该入口读取，并用跨入口测试锁定一致性；不再各自维护正则和回退顺序。

黄金回归继续采用 `available`/`not_available` 双状态和闭世界比较，并在案例及汇总结果中附加统一的 `comparison_status`/`overall_comparison_status`：`PASS`、`FAIL`、`PENDING`、`INCOMPARABLE`；旧的 `status` 字段保留供现有读取器兼容，但所有新读取面必须以 canonical 状态为准。登记表必须声明 `registry_kind`，每个案例必须有 `case_version`；不可用案例必须有 reason；`synthetic_demo` 只能引用 `examples/demo/`，`anonymized_real_project` 的可用输入只能位于 `tests/anonymized_golden_cases/assets/`，且必须有授权、脱敏、核验人/时间/说明 provenance。`available` 案例的每个输入必须包含相对路径、类型、方向和严格 64 位十六进制 `sha256`，执行时无条件复核文件哈希；登记表损坏会转为 `INCOMPARABLE`，不让发布清单裸抛异常。私有资料仍只能由 `real_acceptance_run.py` 在 `local_private_data/` 副本上运行，不能登记到公开黄金库；缺失资料只能使本地验收结果 pending。

发布清单性能项只有在顶层 `status=completed`、配置规模严格为 10000/50000/200000、三条唯一结果均完成、八个必需阶段名称唯一且均完成、Excel 导出阶段含本次规模现场中真实存在且字节数/SHA-256一致的文件时才可为 `passed`；内外报告均走同一严格验证器，取消现场统一为 `conditional`，配置损坏或证据缺失不得通过。真实验收的 Excel+Word 导出按整体处理，Word 失败会清理本次已生成的 Excel 并把数据库登记标为历史/失效；导出函数写出半文件后抛异常也会扫描并清理本次新文件；损坏人工决定或 done marker 记录结构化失败，带 `pipeline_error` 的失败 marker 在修复后可同 run 重试，允许批次继续。

## 4. 验收标准

1. 对临时清单构造一个缺失副本和一个哈希不符副本，`main()` 不抛出断言；生成新的 run 目录和 `acceptance_results.json`，两项均为 pending，未调用解析/计算/导出。
2. 对已有完整临时 corpus，当前全量 **721** 项 pytest 及验收执行器非破坏性、可恢复测试保持通过；该数量随后续回归测试增加而变化，发布清单以实际运行结果为准。
3. A/B/C 回归能证明“解析行集错误但 A/B 同源”或“控制值/范围证据缺失”不会得到绿色 sufficient。
4. `golden_regression.py --json` 仍通过，故意改变 expected 时仍返回差异且不写回 registry。
5. Ruff、`git diff --check`、发布一致性检查通过；性能空报告/缺规模/跳过导出/伪造导出/重复阶段/畸形阶段结构、黄金缺 SHA-256/演示冒充真实、损坏 marker/人工决定、失败 marker 重试和成对导出/半文件失败均有失败测试；不改版本、不生成 Release、不修改私有资料和历史报告。

## 5. 失败处理与恢复

- 预检 pending 不是成功结论；用户补齐或重新校正副本后，可在新 run 或同一 run 续跑。明确带 `pipeline_error` 的失败 marker 不得永久跳过，修复输入或决定后同一 run 可重新处理；已经达到可复用终态的结果才跳过。
- 运行中异常仍保留 run 目录和已完成 marker；不删除、不覆盖旧 run。
- 若发现实现需要改变结算模型或公开 schema，停止本批次并先补充设计，不以兼容层或默认值掩盖。
