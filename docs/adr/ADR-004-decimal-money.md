# ADR-004: 金额一律 Decimal，禁止 float
状态: Accepted | 日期: 2026-08-29
## 决策
数量/单价/金额/税率/累计/加权平均全部使用 decimal.Decimal（ROUND_HALF_UP）。
float 仅允许用于统计图示、模糊匹配得分等非金额路径。
## 理由
- 工程结算对分位敏感；float 二进制误差会污染对账差异判定。
- 差异"报告"与差异"调平"分离：容差只用于报告，不用于改数。
## 强制手段
engine/money.py 提供唯一入口（to_decimal、money_add、money_mul、wavg…）；
单元测试 + hypothesis property-based 覆盖；review 检查金额路径无 float 混入。
