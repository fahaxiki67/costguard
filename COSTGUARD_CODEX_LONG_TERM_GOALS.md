# CostGuard / 价盾 —— CodeX 长期开发目标与不可违背原则

> 适用项目：`fahaxiki67/costguard`
> 基线版本：v0.1.20
> 用途：作为 CodeX / Luna 等编程模型长期开发时的“项目宪章”。
> 原则：每次开始新任务、修改核心逻辑、数据库、OCR、AI、导出、合同规则、控制基准前，先重新阅读本文件。

---

## 一、项目最终目标

价盾不是“AI 自动算造价”的工具，而是一个：

**以原始证据为基础、以确定性计算为核心、以人工确认关键业务事实、以 AI 作为辅助提取与解释能力的造价审计 / 经营合规工具。**

最终任何一个关键金额、异常、控制结论，都必须能够反向追溯到：

1. 原始资料；
2. Evidence 证据；
3. 已确认的业务事实或合同规则；
4. 确定性计算过程；
5. 最终报告或导出结果。

如果任一环节缺失，系统不得伪装成“已确认”。

---

## 二、长期不可违背的 12 条原则

### 1. 原始资料不可修改
任何 Excel、PDF、Word、图片等原始资料只能读取，不得原地修改。

### 2. 缺失值绝不能当 0
以下情况必须显式区分：
- 缺失
- 无法确认
- 不可比较
- 待人工确认
- 解析失败
- OCR 失败

绝不能为了继续计算而自动填 0。

### 3. 不允许人为调平
禁止为了让上下结算、合同金额、报表金额“对得上”而：
- 自动补值；
- 自动修改工程量；
- 自动调整单价；
- 自动合并项目；
- 自动删除差异。

### 4. 金额必须确定性计算
工程量、单价、金额、税额、费率计算必须使用确定性程序和 Decimal。

LLM 不得作为金额真值来源。

### 5. Evidence 必须贯穿全链条
任何重要事实、合同条款、控制金额、异常，都必须保存 Evidence。

Evidence 应尽量包含：
- 文件；
- Sheet / 页码；
- 行列 / bbox；
- 原文；
- hash；
- 提取方式。

### 6. Fail-Closed
资料未解析完整、关键 OCR 页失败、合同事实未确认、控制基准冲突时：

系统不得输出“无异常”“已通过”之类确定性结论。

### 7. AI 只能辅助，不能代替确认
AI 可以：
- 提候选；
- 分类；
- 解释；
- 摘要；
- 草拟报告。

AI 不可以：
- 自动确认合同事实；
- 自动决定控制基准；
- 自动决定两个清单一定相同；
- 自动改变金额；
- 自动认定违规；
- 自动认定责任人。

### 8. 人工确认结果优先
已经人工确认的映射、事实、规则，AI 不得静默覆盖。

### 9. 不可比较必须明确标记
范围、税口径、币种、期间、合同层级不同的数据不得强行比较。

使用：
- INCOMPARABLE
- PENDING
- CONTROL_CONFLICT

而不是 PASS。

### 10. 历史记录不可篡改
Run Contract、Evidence、已执行审计结果应保留历史版本。

不得直接覆盖旧记录使历史审计失真。

### 11. 不得静默联网
合同、结算资料、审计资料默认按敏感业务数据处理。

任何云端 AI、远程 OCR、模型 API：
- 必须用户显式启用；
- 必须明确提示数据会离开本机；
- 默认禁用。

### 12. 不为“AI 功能更多”牺牲可靠性
如果 AI 功能与证据链、确定性、可复核性冲突：

优先保留可靠性。

---

## 三、当前版本的真实定位

v0.1.20 已具备较好的底层方向，包括：

- Decimal 金额计算；
- 原文件只读；
- Evidence；
- Run Contract；
- Fail-Closed 思路；
- SheetConfirmDialog 人工确认；
- Excel / CSV / XLS 基础处理；
- A/B/C 校核框架；
- 合同确定性初步抽取。

但仍不能视为完整生产版。

当前主要未闭环能力：

1. OCR；
2. 混合 PDF 页级完整性；
3. 合同“候选事实 → 人工确认 → 生效事实”；
4. 控制基准冲突；
5. 50k+ / 200k Excel 导出；
6. Mac / Windows + WPS / Excel 真实验证；
7. 本地免费 AI Provider；
8. 真实脱敏 Golden Cases。

---

## 四、长期开发顺序

不得同时大范围推进多个核心子系统。

推荐顺序：

### 阶段 A：状态与文档一致性
目标：
让源码、ROADMAP、Release、CURRENT_STATE_AUDIT 对当前能力的描述一致。

要求：
- 不重复开发已经存在的功能；
- 老 Issue 若已代码修复，应改为“待实机验证”；
- 区分：
  - implemented
  - verified
  - production_verified
  - pending
  - blocked

---

### 阶段 B：页级 OCR

这是当前最高优先级技术任务。

架构必须建立：

```text
PdfRenderer
↓
PageExtractionResult
↓
OcrProvider
↓
Evidence
↓
Document Parser
```

必须按“页”判断，而不是只判断整个 PDF 是否有文本。

典型错误必须避免：

一个 30 页 PDF：
- 1~20 页有文本；
- 21~30 页是扫描件。

不能因为前 20 页可读取，就把整个 PDF 判定为解析成功。

页面状态至少支持：
- native_text
- ocr
- pending_ocr
- ocr_failed
- needs_review

#### OCR 推荐路线

默认轻量 OCR：
**RapidOCR + ONNX Runtime**

增强 OCR：
**PaddleOCR / PP-OCR / PP-Structure**

架构必须通过 `OcrProvider` 隔离具体 OCR 引擎。

业务代码不得直接依赖 RapidOCR 或 PaddleOCR。

不要静默下载 OCR 模型。

模型必须记录：
- model_id
- version
- sha256
- source
- license
- language
- expected_size

---

### 阶段 C：合同事实确认

合同抽取结果不能直接成为正式业务规则。

完整生命周期：

```text
原文
↓
candidate fact
↓
人工确认 / 拒绝
↓
confirmed fact
↓
Run Contract / 计算规则
```

至少支持：
- candidate
- confirmed
- rejected
- needs_review

只有 `confirmed` 才能用于金额和控制规则。

历史旧数据如果无法证明人工确认过：

不得自动变成 confirmed。

#### 特别注意

不能只在一整段文字中寻找第一个：

- X 天；
- X%；
- 某个金额。

例如：

> 承包人 7 日提交，监理 14 日审核，发包人 28 日付款。

不能把 7 日误认为付款时限。

必须使用：
- 触发词；
- 局部窗口；
- clause span；
- 主体；
- 动作；
- 数值；
- Evidence。

---

## 五、费率不是一个百分比

遇到管理费、措施费、税费、扣款比例等：

不能只保存：

```text
3%
```

必须尽量结构化为：

- rate
- base_type
- base_definition
- tax_basis
- inclusions
- exclusions
- cap
- floor
- effective_scope
- effective_period
- priority
- Evidence
- review_status

因为：

“按结算价 3%”

与：

“按不含甲供材税前建安费 3%”

不是同一个计算规则。

---

## 六、控制基准

必须正式区分：

- reference
- control_candidate
- settlement_result

不能用下面这些粗暴规则：

- 最新日期自动优先；
- 最大金额自动优先；
- 最小金额自动优先；
- 最新文件自动覆盖旧文件。

控制基准至少应考虑：

- scope
- tax_basis
- currency
- effective_period
- version
- evidence
- supersedes
- review_status
- priority

输出状态至少支持：

- PASS
- FAIL
- PENDING
- INCOMPARABLE
- CONTROL_CONFLICT

规则：

1. 范围不同 → INCOMPARABLE
2. 税口径不同 → INCOMPARABLE
3. 币种不同 → INCOMPARABLE
4. 未确认 → PENDING
5. 两个有效基准冲突且无替代关系 → CONTROL_CONFLICT

超过控制基准可以提示：

> “结算结果较已确认控制基准高 X 元”

但不能自动进一步认定：
- 违规；
- 责任人；
- 违规金额。

除非有额外明确规则和 Evidence。

---

## 七、Excel 大数据导出

不要直接重写 `excel_export.py`。

先 profiling。

必须统计：
- DB query time
- object build time
- worksheet population time
- style time
- autowidth time
- Evidence hyperlink time
- save time
- peak RSS
- output size

测试规模：
- 10k
- 50k
- 200k

优先优化：
- 减少整表重复遍历；
- autowidth 使用有界样本；
- 样式在写入时处理；
- Evidence hyperlink 随行生成；
- 数据分批读取；
- 尽量减少 openpyxl 私有接口。

如果 profiling 证明 openpyxl 是主要瓶颈，再考虑：

**XlsxWriter constant_memory backend**

但不得未经 WPS / Excel 实机验证就完全替换 openpyxl。

---

## 八、本地免费 AI 的长期架构

不要把某个具体模型写死。

建立：

```text
AIProvider
├── DisabledProvider
└── OpenAICompatibleProvider
```

通过 localhost OpenAI-compatible API 兼容：

- Ollama
- llama.cpp server
- LM Studio
- 其他兼容实现

推荐本地模型：

低资源：
- Qwen3.5 4B

更推荐：
- Qwen3.5 9B

模型只是默认建议，不能硬编码。

程序不得：
- 静默安装 Ollama；
- 静默下载数 GB 模型；
- 默认上传业务文件。

---

## 九、AI 可以做什么

允许：

- 文档类型候选；
- 合同条款候选；
- 字段候选；
- 同义清单候选；
- 风险说明草拟；
- Evidence 摘要；
- 报告文字草拟。

AI 输出必须保存：

- provider
- model
- model_version
- prompt_version
- schema_version
- input_hash
- output
- Evidence IDs
- review_status

优先要求严格 JSON Schema。

Schema 不合法：
→ reject

引用 Evidence 不存在：
→ reject

无法确认：
→ needs_review

不得自动猜值继续计算。

---

## 十、AI 永远不能做什么

禁止：

- 最终工程量；
- 最终单价；
- 最终金额；
- 最终税额；
- 最终控制基准；
- 自动确认清单对应关系；
- 自动启用费率；
- 自动覆盖人工确认；
- 自动认定违规；
- 自动认定责任。

---

## 十一、Embedding 暂不作为当前主任务

现阶段不要优先加入复杂向量数据库和 embedding 体系。

当前更重要的是：

1. OCR；
2. 合同事实；
3. 控制基准；
4. Evidence；
5. 性能；
6. Golden Cases。

未来可以实验 BGE-M3 等 embedding。

Embedding 只能用于：

> 推荐可能相同的清单项

不能用于：

> 自动认定两个清单项相同

最终仍需确定性规则或人工确认。

---

## 十二、Golden Cases

正式生产前必须建立脱敏测试项目。

至少覆盖：

### Excel
- 多 Sheet
- 隐藏行列
- 筛选
- 合并单元格
- 公式
- xls
- xlsx
- 10k
- 50k
- 200k

### PDF / OCR
- 全文本 PDF
- 全扫描 PDF
- 混合 PDF
- OCR 失败页
- 旋转页
- 空白页
- 低质量文本层

### 合同
- 同一段多个天数
- 同一段多个百分比
- 相同百分比但不同计算基数
- 补充协议覆盖原合同
- 两个控制基准冲突

### 结果状态
必须验证：
- PASS
- FAIL
- PENDING
- INCOMPARABLE
- CONTROL_CONFLICT

禁止为了让测试通过而自动更新 golden expected。

---

## 十三、真实环境生产验证

不能只以 pytest 通过作为“生产可用”。

必须逐步验证：

### macOS Apple Silicon（P0）
- WPS
- Excel（如可用）

### Windows x64（P1）
- WPS
- Excel

至少测试：

- 打开；
- 重算；
- 筛选；
- 超链接；
- 打印；
- 保存；
- 再打开；
- 数据一致性。

OCR 也必须验证真实打包后的 App / exe，不仅是开发环境 `pip install` 成功。

---

## 十四、代码结构原则

当前部分文件已经较大。

不要进行一次“大爆炸式重构”。

原则：

1. 修改哪个功能，就只抽取与该功能直接相关的职责。
2. 先写 characterization tests。
3. 再抽模块。
4. 每次抽取后全量测试。
5. 不要同时修改架构和业务算法。
6. 数据库变化必须有 migration。
7. migration 必须兼容历史 workspace。
8. 不得删除历史 Evidence。
9. 不得原地篡改历史 Run Contract。

---

## 十五、CodeX 每次任务的固定流程

每次开始任务必须：

### 第一步：先观察
阅读：
- AGENTS.md
- 本文件
- README.md
- ARCHITECTURE.md
- ROADMAP.md
- CURRENT_STATE_AUDIT.md
- 当前 Release
- 相关 Issue
- 相关源码
- 相关测试

### 第二步：确认当前真实状态
不要根据旧文档猜。

先回答：

1. 现在已经有什么？
2. 哪些只是部分实现？
3. 哪些是完全没实现？
4. 哪些只是缺实机验证？

### 第三步：再修改
尽量最小修改。

### 第四步：验证
必须运行相关测试。

关键模块修改后运行全量测试。

### 第五步：报告
每个阶段完成后必须输出：

1. 修改文件；
2. 修改原因；
3. 原问题；
4. 解决方式；
5. 新增 / 修改测试；
6. 测试结果；
7. 未验证事项；
8. 风险；
9. 是否改变数据口径；
10. 是否需要 migration；
11. 回滚方法；
12. 下一步。

---

## 十六、必须停止并报告的情况

如果出现以下情况，不允许 CodeX 自己猜：

- 会改变金额计算口径；
- 会改变控制基准定义；
- 会把旧数据自动升级为 confirmed；
- 会删除历史 Evidence；
- 会修改原文件；
- 会默认上传资料到互联网；
- 无法判断某合同规则实际含义；
- 两份资料范围不可比较；
- 为了性能必须牺牲证据链；
- migration 存在历史数据丢失风险；
- Golden Case 与当前结果冲突但原因不明。

此时应停止该项修改，报告：
- 冲突点；
- 证据；
- 可选方案；
- 风险。

---

# 当前推荐版本路线

## v0.1.21
目标：
- 当前状态文档整理；
- 页面级 OCR 骨架；
- RapidOCR Provider；
- 混合 PDF 不漏页；
- OCR Evidence。

## v0.1.22
目标：
- 合同 candidate fact；
- 人工确认；
- confirmed fact 才能生效；
- 多天数 / 多百分比抽取改进。

## v0.1.23
目标：
- ControlCandidate；
- supersedes；
- INCOMPARABLE；
- CONTROL_CONFLICT。

## 后续版本
依次：
1. 50k / 200k 导出性能；
2. WPS / Excel 双平台实机；
3. AIProvider；
4. 本地 Qwen；
5. Golden Cases 扩展；
6. 再考虑 embedding / 更复杂智能能力。

---

# 最终判断标准

不要用“新增了多少功能”衡量价盾是否进步。

每次版本发布只问：

1. 是否更不容易漏数据？
2. 是否更不容易把未知当已知？
3. 是否更不容易算错？
4. 是否更容易追溯 Evidence？
5. 是否更容易人工复核？
6. 是否更容易解释为什么得到这个结果？
7. 是否在真实 WPS / Excel / Mac / Windows 环境下可复现？

只有这些指标改善，才属于有效升级。
