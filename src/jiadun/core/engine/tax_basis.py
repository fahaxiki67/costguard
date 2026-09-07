"""税口径模型（任务书任务 C）。

规则（§C1-C5）：
- 「单价通常不含税」只是业务候选，无明确文本证据时口径必须保持 unknown；
- included/excluded 只能来自两类依据：表头明确文本（auto，如「含税单价」
  「不含税合价」「价税合计」）或人工标注（human，理由必填写 Evidence）；
- 独立税金/税额列的存在只登记事实（line_items.tax_amount），不推断口径；
- 税率数值（如 9%）不是金额口径，永不据此推断含税/不含税；
- 口径不一致的两个期次在没有合法转换依据时不可直接比差额
  （tax_basis_comparable 返回不可比原因，比较层据此降级 PENDING/INCOMPARABLE）。
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime

from jiadun.core.evidence import evidence as evidence_api

TAX_BASIS_UNKNOWN = "unknown"
TAX_BASIS_INCLUDED = "included"
TAX_BASIS_EXCLUDED = "excluded"

TAX_BASES = (TAX_BASIS_UNKNOWN, TAX_BASIS_INCLUDED, TAX_BASIS_EXCLUDED)

BASIS_ZH = {
    TAX_BASIS_UNKNOWN: "未确认",
    TAX_BASIS_INCLUDED: "含税",
    TAX_BASIS_EXCLUDED: "不含税",
}

# 明确文本依据（C3）。只认表头/Sheet 名里的显式说法，税率数字不算。
# 注意「含税」是「不含税/未含税」的子串：included 侧用负向后顾排除否定前缀。
_EXCLUDED_PATTERN = re.compile(r"不含税|未含税|税前|除税")
_INCLUDED_PATTERN = re.compile(r"(?<![不未])含税|价税合计|税价合计")
# 独立税金列特征（登记事实用，不推断口径）
_TAX_AMOUNT_PATTERN = re.compile(r"税金|税额|增值税额")


def detect_tax_basis(header_text: str) -> tuple[str, str]:
    """按表头/Sheet 名的明确文本给出（口径, 依据说明）。

    同时命中含税与不含税说法时判为冲突→unknown（原因写明冲突，待人工）。
    """
    text = str(header_text or "")
    if not text:
        return TAX_BASIS_UNKNOWN, "无任何税信息文本"
    excluded = _EXCLUDED_PATTERN.search(text)
    included = _INCLUDED_PATTERN.search(text)
    if excluded and included:
        return (
            TAX_BASIS_UNKNOWN,
            f"表头同时出现「{excluded.group(0)}」与「{included.group(0)}」，"
            "口径冲突待人工确认",
        )
    if excluded:
        return TAX_BASIS_EXCLUDED, f"表头明确标注「{excluded.group(0)}」"
    if included:
        return TAX_BASIS_INCLUDED, f"表头明确标注「{included.group(0)}」"
    if _TAX_AMOUNT_PATTERN.search(text):
        return (
            TAX_BASIS_UNKNOWN,
            "检测到独立税金/税额列；仅登记事实，口径需人工确认",
        )
    return TAX_BASIS_UNKNOWN, "无任何税信息"


def tax_basis_comparable(basis_a: str, basis_b: str) -> tuple[bool, str]:
    """两个口径是否可直接比较差额（C4）。

    未知必须先确认（PENDING）；一致可直接比；不一致无转换依据不可比
    （INCOMPARABLE）。转换依据（合同/规则/人工确认）由调用方另行核验后
    才允许覆盖本函数结论。
    """
    a, b = str(basis_a or TAX_BASIS_UNKNOWN), str(basis_b or TAX_BASIS_UNKNOWN)
    if a == TAX_BASIS_UNKNOWN or b == TAX_BASIS_UNKNOWN:
        return False, (
            f"存在未确认税口径（{'A' if a == TAX_BASIS_UNKNOWN else ''}"
            f"{'与' if a == TAX_BASIS_UNKNOWN and b == TAX_BASIS_UNKNOWN else ''}"
            f"{'B' if b == TAX_BASIS_UNKNOWN else ''}），需先人工确认 → PENDING"
        )
    if a == b:
        return True, "双方口径一致，可直接比较"
    return False, (
        f"口径不一致（A={BASIS_ZH[a]}，B={BASIS_ZH[b]}）且无合法转换依据，"
        "不得直接计算价差 → INCOMPARABLE"
    )


def set_sheet_tax_basis(
    conn: sqlite3.Connection,
    project_id: int,
    sheet_id: int,
    basis: str,
    *,
    reason: str = "",
    reviewed_by: str = "user",
) -> dict:
    """人工标注 Sheet 税口径；理由必填，写审计 Evidence。

    口径变更会随 raw_sheets 快照进入 Run Contract（任务书 C6-10：旧运行
    因税口径变化失效，不得继续使用旧结果）。
    """
    if basis not in TAX_BASES:
        raise ValueError(f"未知的税口径：{basis}")
    if not (reason or "").strip():
        raise ValueError("标注税口径必须填写理由（写入审计）")
    row = conn.execute(
        """SELECT rs.sheet_name, rs.tax_basis, rs.tax_basis_source,
                  sf.original_name, sf.sha256
           FROM raw_sheets rs
           JOIN parse_batches pb ON pb.id=rs.batch_id
           JOIN source_files sf ON sf.id=pb.file_id
           WHERE rs.id=? AND sf.project_id=?""",
        (int(sheet_id), int(project_id)),
    ).fetchone()
    if row is None:
        raise ValueError(f"Sheet 不存在或不属于当前项目：sheet_id={sheet_id}")
    before = row["tax_basis"] or TAX_BASIS_UNKNOWN
    now = datetime.now().isoformat(timespec="seconds")
    with conn:
        conn.execute(
            """UPDATE raw_sheets SET tax_basis=?, tax_basis_source='human',
               tax_basis_reason=?, tax_basis_updated_at=?, tax_basis_actor=?
               WHERE id=?""",
            (basis, (reason or "").strip(), now, reviewed_by, int(sheet_id)),
        )
        evidence_api.add_evidence(
            conn, int(project_id), "sheet_tax_basis",
            f"Sheet「{row['sheet_name']}」税口径：{before} → {basis}"
            f"（{(reason or '').strip()}）",
            steps=[{
                "step": "人工标注税口径",
                "sheet_id": int(sheet_id),
                "source_file": row["original_name"],
                "source_sha256": row["sha256"],
                "before": before,
                "before_source": row["tax_basis_source"],
                "after": basis,
                "reviewed_by": reviewed_by,
                "reason": (reason or "").strip(),
            }],
            sources=[{
                "sheet_id": int(sheet_id),
                "sheet_name": row["sheet_name"],
                "source_file": row["original_name"],
                "source_sha256": row["sha256"],
            }],
            commit=False,
        )
    return {"sheet_id": int(sheet_id), "before": before, "after": basis}
