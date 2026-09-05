# 接力三轮阶段报告：OCR 质量回归 / 导出性能 / 金样本沉淀（2026-09-06）

## 任务来源

用户接力任务书（第三轮）五项主攻：消化前两轮遗留、OCR 质量回归、
Excel 大导出先 profiling 后优化、Golden Cases 沉淀、UI 打磨。

## 前两轮遗留消化（结论：已全部收口，无需返工）

- 前两轮成果（v0.1.26 性能修复、结论性框架 schema v54、市场实测修复
  fa4677a、golden 基线 evidence 计数更新 680ae96）全部已提交。
- 接力起点验证：`golden_regression --json` status=passed；ruff 全绿；
  全量单元测试退出码 0（无此前接力二轮报告所述的 8 项失败——那批
  失败源于当时并行会话未提交改动，现已随 fa4677a/680ae96 收口）。
- CURRENT_STATE_AUDIT 未尽事项核对：T14–T18 真实语料副本仍缺、四环境
  Office 验证与签名公证仍未闭合——属外部资源门槛，本轮未动、如实保留。

## 主攻 2：OCR 质量回归用例集

新增 `tests/unit/test_ocr_quality_regression.py`（23 项）：

- 输入页形态 → 页状态全矩阵（native_text/ocr/pending_ocr/ocr_failed/
  needs_review 十个命名用例，含「文本层+图片并存拒仅信文本层」「低置信
  进人工确认」「模型身份缺失不可追溯即拒绝」）；
- 置信度阈值边界（恰好达标 vs 收紧阈值降级）；
- 覆盖完整性 fail-closed（缺尾页/多页/乱序/零页）与混合文档整份 Pending；
- 数据结构不变量（置信度越界、可解析页空文本、未知状态均构造期拒绝）；
- **RapidOCR 实机质量回归**（`JIADUN_TEST_REAL_OCR=1` 门控）：真实渲染
  中文页 → 真实识别 → 置信度 0.97≥0.80 → 接入管线落 ocr 状态且带
  模型身份。CI 默认跳过并注明原因，不用假结果冒充实机。
- 实测发现并修正用例设计错误一处：页级 model_id 缺失会被 provider 级
  describe() 元数据合法回填——「不可追溯」用例必须两级同时缺失。

### OCR 增强 provider（Paddle 骨架）

`src/jiadun/platform/ocr.py` 新增 `PaddleOcrProvider`（显式启用，非默认）：

- 必须显式提供本地模型目录 + 逐文件 SHA-256 清单 + model_id/version；
  未安装/目录缺失/文件被替换/未知角色一律 OcrProviderUnavailable，
  绝不自动安装或下载（无网行为在适配层层面成立）；
- det/rec/cls 严格角色词表 → 引擎模型目录指向已校验本地目录，
  3.x/2.x 双参数签名回退；
- 2.x 行形态 / 3.x rec_texts 对象 / 扁平形态 → OcrResult；畸形行跳过，
  置信度取最低分（保守）；
- 14 项伪造 paddleocr 模块测试全绿；默认工厂只回 RapidOCR（换引擎必须
  显式，防静默切换改变识别结果）。
- **边界（如实）**：真实 paddleocr 引擎行为（引擎是否接受本地目录参数、
  实际不触发下载、3.x 返回形态）本机未安装无法验证，标记 PENDING/
  【需要人工处理】；骨架的契约由伪造模块测试锁定。

## 主攻 3：Excel 大导出性能（先 profiling 后优化，实测数据）

### 基线（同机同种子 10k 合成项目，performance_benchmark）

导入 26.4s / 异常 4.2s / 匹配 0.7s / 双向校核 38.8s / **导出 144.7s**
（峰值 304MB）——导出是最大瓶颈，与 09-02 现场「50k 导出被取消」一致。

### 阶段内 profiling（新增 scripts/export_profile.py，宪章 §七口径）

- 阶段计时 + cProfile 热点 + tracemalloc/RSS/产物大小，产品代码零改动；
- 实锤：导出一次触发 **22 次** `current_results_available` 门控，热点链
  `raw_cell_digest → canonical_json` 达 **441 万次**序列化（102s）、
  `_line_item_digest` 49 次全量重算（49s）、build_model 94.5s；
- v53 的 `sheet_cell_digests` 持久表**只有读方没有写方**（缓存从未生效）。

### 优化（src/jiadun/core/contracts/run_contract.py）

进程内按连接的**只读窗口缓存**：以（数据库文件路径 + PRAGMA data_version
+ conn.total_changes）为写指纹——指纹相等蕴含数据库零写入，Sheet/明细
摘要等纯函数值跨门控调用复用；任何写入（导入/人工确认/外部直改库）立即
全部失效重算。

- **中途废弃的方案（重要工程决策记录）**：曾实现持久摘要表写入 + 读端
  cell_count 核验，被既有特征测试
  `test_verification_rejects_raw_cell_content_drift`（绕过触发器直改
  raw_cells 值必须被读路径发现）当场否决——盲信跨进程持久缓存会把值级
  漂移隐藏到缓存失效为止，违反 Fail-Closed。进程内 + 写指纹方案保留
  全部漂移可见性（该测试及跨连接写入失效均有专项测试）。
- XlsxWriter 替换 openpyxl：**未做**——profiling 证明序列化只占 20.7s
  且宪章要求 WPS/Excel 实机验证后才可替换，瓶颈本来也不在写入引擎。

### 实测前后对比（同机同种子 10k，端到端基准）

| 阶段 | 优化前 | 优化后 | 变化 |
| --- | ---: | ---: | ---: |
| Excel 审核底稿导出 | 144.7s | **26.6s** | **-82%** |
| 对上/对下双向校核 | 38.8s | **14.1s** | -64% |
| Excel 合成导入 | 26.4s | 26.0s | 持平 |
| 峰值内存（导出） | 304.7MB | 297.1MB | 持平 |

profiling 口径（含 cProfile 开销）：export_workbook 239.4s → 57.9s
（-76%）；build_model 94.5s → 10.9s；产物字节 4,466,290 B 完全一致。
50k 基准（优化后代码，实测完整通过——此前该规模导出曾被人工取消）：
合成导入 304.0s / 异常 22.9s / 匹配 3.9s / 双向校核 73.8s /
导出 141.0s。导入与异常为既有成本非本轮目标，仍待后续优化；
200k 规模未测（历史未完成，维持 PENDING）。

### 新增回归

- `tests/unit/test_sheet_digest_memo.py` 6 项：只读窗口复用（计数补丁
  观测零重算）、本连接写入失效、绕过触发器值级漂移保持可见、跨连接
  写入失效（data_version 路径）、容量上限守卫、明细摘要同款行为。

## 主攻 4：Golden Cases——三轮实测教训沉淀为金样本

- `scripts/generate_demo_data.py` 新增合成语料
  `演示-市场实测教训-合同摘录-合成.docx`（确定性生成，既有 4 文件字节
  不变）：人民币大写金额（含角分/负号，用市场实测值 183199873.25 /
  -179527788）、当事人标签-值邻接三形态 + 守卫噪声行（项目部）、
  GB50500-2013 规范编号、同段多天数/多百分比。
- 注册 `tests/golden/cases.json` 第二个可用案例 `market_lessons_v1`：
  期望指标来自实跑并逐条人工核对（7 条 contract_fact 证据 = 2 金额 +
  2 当事人 + 3 时限候选，全部 review=candidate；非自动更新既有基线）。
- `tests/unit/test_market_lessons_contract.py` 4 项单元级锁定：事实值
  逐条精确断言、candidate 生命周期、噪声行零候选、manifest/SHA256SUMS
  收录。黄金回归双案例 PASS。
- 已知候选级限制如实登记（时限候选取首个天数，人工复核门控兜底），
  记录于 GOLDEN_BASELINE_CHANGELOG_v0.1.25.md。

## 主攻 5：UI 打磨

- 工作台拖拽区文案改为造价业务语言：「将结算书、合同或整个资料文件夹
  拖到这里（递归导入，逐项人工分类确认，原文件不会被修改）」，去掉
  含糊的「打包资料」；无测试锁定旧文案。
- `test_ui_import_flow.py` 补 4 项边界：扩展名大小写不敏感分类、隐藏
  系统文件静默忽略但 zip 类不支持的跳过原因明细、项目命名建议
  （资料夹名/单文件名/父目录/兜底）四分支。

## 修改文件清单

产品代码：`src/jiadun/core/contracts/run_contract.py`（只读窗口缓存 +
共享摘要函数）、`src/jiadun/platform/ocr.py`（Paddle provider）、
`src/jiadun/ui/workbench.py`（文案）。
脚本：`scripts/export_profile.py`（新增）、`scripts/generate_demo_data.py`
（金样本语料）。
数据/登记：`examples/demo/`（新 docx + manifest/SHA256SUMS）、
`tests/golden/cases.json`（新案例）、GOLDEN_BASELINE_CHANGELOG。
测试：新增 4 个测试文件（47 项），扩展 test_ui_import_flow.py（4 项）。

## 未验证事项 / 风险

1. 真实 PaddleOCR 引擎行为（本机未安装）——PENDING，需人工装引擎实机验证；
2. 200k 规模本轮未跑（50k 已实测通过；200k 导入侧历史未完成，与导出
   优化无关，仍为 PENDING）；
3. WPS/Excel 真机打开导出文件验证未做（优化不改导出内容——产物字节
   一致已证明，但四环境门槛本身未闭合）；
4. 只读窗口缓存在多线程并发写场景下的行为：写指纹在 _sheet_scope 入口
   核验，窗口内并发写理论上存在极窄复用窗口；项目库为单写者模型
   （SQLite 锁），现有 UI 均单线程访问项目连接，风险可控并已注释说明；
5. OCR 实机用例依赖本机字体（macOS 系统字体存在故通过；无中文字体
   环境按跳过处理）。

## 数据口径 / 迁移 / 回滚

- 零数据库 schema 变更、零业务计算口径变更；导出产物字节级一致。
- 回滚：还原 run_contract.py 即回到逐次全量重算行为（性能退回基线，
   行为不变）；其余均为增量文件，删除即回滚。
