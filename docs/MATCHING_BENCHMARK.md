# 价盾匹配 Benchmark

本文件定义匹配结果的验收口径。Benchmark 只评价现有匹配引擎的输出，不参与
金额计算、匹配决策或正式结算结论；没有人工真值时，报告必须保持
`PENDING`。

## 标签边界

每个案例都要由业务人员在脱敏副本上确认项目项身份和等价关系。身份不能使用
临时 SQLite 自增 `line_items.id`，推荐使用：

```text
sha256:<源文件 SHA-256>|sheet:<JSON 编码的 Sheet 名>|row:<物理行号>
```

可以用 `scripts/matching_benchmark.py` 中的 `stable_item_identity()` 生成。源
文件、Sheet、物理行和原始字段仍必须通过 Evidence 回溯；名称相似度不能替代
人工真值。

真值 JSON 的最小结构如下：

```json
{
  "item_universe": ["item-a", "item-b", "item-c"],
  "matching_groups": [["item-a", "item-b"]],
  "unmatched_items": ["item-c"],
  "incomparable_items": [],
  "pending_items": []
}
```

五个分区必须合起来恰好覆盖 `item_universe`，每个身份只能出现一次。缺少
分区、重复身份、未知身份或把不可比/待补资料项放入真值匹配组时，结果为
`INCOMPARABLE`，不会继续计算指标。

## 评价口径

预测输入为带置信档位的分组：

```json
{
  "items": ["item-a", "item-b"],
  "level": "probable",
  "status": "pending"
}
```

只有 `confirmed` 和 `probable` 形成自动预测正样本；`suspected` 只计入疑似
和人工复核数量，不当作自动匹配。`incomparable` 与 `pending_data` 只能保留
为非可比/待补资料状态。

报告按项目项对输出：

- `precision`：预测为匹配的项目项对中，真正匹配的比例；
- `recall`：人工确认匹配的项目项对中，被预测覆盖的比例；
- `f1`：精确率与召回率的调和平均；
- `false_positive` / `false_positive_pairs`：错误合并数量和具体项目项对；
- `false_negative` / `false_negative_pairs`：漏匹配数量和具体项目项对；
- `automatic_confirmation_count`、高概率/疑似/不可比/待补资料数量和人工复核数量。

比例由 `Decimal` 计算并以四位小数字符串写入 JSON。误报项目对单独列示，
优先级高于追求自动匹配率。

## 执行方式

准备一个只含脱敏标签和算法预测的 JSON：

```json
{
  "cases": [
    {
      "case_id": "sanitized-case-v1",
      "truth": {
        "item_universe": ["item-a", "item-b"],
        "matching_groups": [["item-a", "item-b"]],
        "unmatched_items": [],
        "incomparable_items": [],
        "pending_items": []
      },
      "predicted_groups": [
        {"items": ["item-a", "item-b"], "level": "confirmed", "status": "pending"}
      ]
    }
  ]
}
```

运行：

```bash
uv run python scripts/matching_benchmark.py \
  --input /path/to/matching-benchmark.json \
  --output /tmp/jiadun-matching-benchmark.json \
  --markdown /tmp/jiadun-matching-benchmark.md
```

机器报告和 Markdown 报告都保留案例级 `PASS`、`FAIL`、`PENDING` 或
`INCOMPARABLE`。`PENDING` 代表真实案例或人工标签尚未补齐，不代表指标为零，
也不能作为生产发布通过依据。

真实案例登记仍遵循 [真实资料验收说明](REAL_DATA_ACCEPTANCE.md)：原始工程资料
放在 `local_private_data/`，只有经过授权和充分脱敏的副本才能进入
`tests/anonymized_golden_cases/assets/`；基准文件不得写入真实公司、人员或
未脱敏金额。
