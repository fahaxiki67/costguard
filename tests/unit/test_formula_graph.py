"""有界公式影响图的合成测试。

测试只使用人工构造的工作表公式，不读取或修改真实工程资料。重点验证：
支持的 A1 依赖可复算，不支持的动态/外部语义不会被静默推断，容量与源文件
指纹失效都会关闭 complete 门控。
"""

from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook

from jiadun.core.evidence import (
    CellReference,
    FormulaGraphLimits,
    FormulaStatus,
    build_formula_graph,
    parse_formula,
    sha256_file,
)


def addresses(references: tuple[CellReference, ...] | set[CellReference]) -> set[str]:
    return {reference.address for reference in references}


def test_same_sheet_absolute_and_finite_range_are_expanded() -> None:
    result = parse_formula("=SUM($A$1:B2,C$3)", current_sheet="明细")

    assert result.status == FormulaStatus.COMPLETE
    assert addresses(result.references) == {
        "明细!A1",
        "明细!B1",
        "明细!A2",
        "明细!B2",
        "明细!C3",
    }
    assert [item.address for item in result.ranges] == ["明细!A1:B2", "明细!C3"]


def test_quoted_and_escaped_sheet_names_are_decoded_without_losing_scope() -> None:
    result = parse_formula(
        "='O''Brien Sheet'!$A$1+'Sheet 2'!A1:B2",
        current_sheet="Current",
    )

    assert result.status == FormulaStatus.COMPLETE
    assert addresses(result.references) == {
        "O'Brien Sheet!A1",
        "Sheet 2!A1",
        "Sheet 2!B1",
        "Sheet 2!A2",
        "Sheet 2!B2",
    }


def test_reverse_index_covers_same_sheet_cross_sheet_and_transitive_impact() -> None:
    graph = build_formula_graph(
        {
            "Sheet1!B1": "=A1",
            "Sheet1!C1": "=B1",
            "Sheet 2!D1": "='Sheet1'!A1",
        }
    )

    assert graph.status == FormulaStatus.COMPLETE
    assert addresses(graph.get_dependents("Sheet1!A1")) == {"Sheet1!B1", "Sheet 2!D1"}
    impact = graph.impact("Sheet1!A1")
    assert impact.status == FormulaStatus.COMPLETE
    assert addresses(set(impact.dependents)) == {"Sheet1!B1", "Sheet1!C1", "Sheet 2!D1"}
    assert graph.reverse_index[CellReference("Sheet1", 1, 1)]


def test_workbook_input_indexes_formula_cells_only() -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Calc"
    worksheet["A1"] = 10
    worksheet["B1"] = "=A1"
    worksheet["C1"] = "=B1"

    graph = build_formula_graph(workbook, source_file="book.xlsx")

    assert graph.status == FormulaStatus.COMPLETE
    assert addresses(graph.get_dependents("book.xlsx::Calc!A1")) == {"Calc!B1"}
    assert graph.get_node("Calc!B1") is not None


def test_external_named_structured_3d_whole_and_dynamic_references_are_opaque() -> None:
    formulas = (
        "=[Book2.xlsx]Sheet1!A1",
        "=TotalCost+A1",
        "=Table1[Amount]",
        "=Table1[[#This Row],[Amount]]",
        "=Sheet1:Sheet3!A1",
        "=Sheet1!A1:Sheet2!B2",
        "='Jan:Mar'!A1",
        "=A:A",
        "=Sheet1!1:1",
        '=INDIRECT("A1")',
        "=OFFSET(A1,1,0)",
        "={=SUM(A1:A2)}",
    )

    for formula in formulas:
        result = parse_formula(formula, current_sheet="Sheet1")
        assert result.status == FormulaStatus.OPAQUE, formula
        assert result.references == (), formula
        assert result.ranges == (), formula

    assert parse_formula("=[Book2.xlsx]Sheet1!A1", "Sheet1").reason == (
        "external_link_or_structured_reference"
    )
    assert parse_formula("=A:A", "Sheet1").reason == "whole_column_or_row_reference"
    assert parse_formula("=Sheet1:Sheet3!A1", "Sheet1").reason == "three_dimensional_reference"
    assert parse_formula("=INDIRECT(\"A1\")", "Sheet1").reason == "indirect_or_offset"


def test_array_and_shared_metadata_is_not_inferred_from_anchor_cells() -> None:
    assert parse_formula("=A1:A2", "Sheet1", is_array=True).status == FormulaStatus.OPAQUE
    assert parse_formula("=A1", "Sheet1", is_shared=True).status == FormulaStatus.OPAQUE
    assert parse_formula("=A1:A2", "Sheet1", is_array=True).references == ()
    assert parse_formula("=A1", "Sheet1", is_shared=True).references == ()


def test_parse_failures_and_limits_are_incomplete_and_have_no_partial_edges() -> None:
    assert parse_formula("=SUM(A1", "Sheet1").status == FormulaStatus.INCOMPLETE
    assert parse_formula("=A1+", "Sheet1").status == FormulaStatus.INCOMPLETE
    assert parse_formula("=A1:A100", "Sheet1", limits=FormulaGraphLimits(max_range_cells=10)).status == (
        FormulaStatus.INCOMPLETE
    )
    assert parse_formula(
        "=A1+B1",
        "Sheet1",
        limits=FormulaGraphLimits(max_reference_expressions=1),
    ).references == ()
    assert parse_formula(
        "=" + "A1+" * 20,
        "Sheet1",
        limits=FormulaGraphLimits(max_formula_length=12),
    ).reason == "limit_formula_length"


def test_graph_node_and_edge_limits_close_the_graph() -> None:
    node_limited = build_formula_graph(
        {"S!B1": "=A1", "S!C1": "=A2"},
        max_nodes=1,
    )
    assert node_limited.status == FormulaStatus.INCOMPLETE
    assert len(node_limited.nodes) == 1

    edge_limited = build_formula_graph(
        {"S!B1": "=A1", "S!C1": "=A2"},
        max_edges=1,
    )
    assert edge_limited.status == FormulaStatus.INCOMPLETE
    assert addresses(edge_limited.get_dependents("S!A1")) == {"S!B1"}
    assert edge_limited.get_dependents("S!A2") == frozenset()


def test_source_hash_and_version_mismatch_invalidates_graph(tmp_path: Path) -> None:
    source = tmp_path / "source.xlsx"
    source.write_bytes(b"v1")
    digest_v1 = sha256_file(source)
    graph = build_formula_graph(
        {"S!B1": "=A1"},
        source_file=str(source),
        source_file_hash=digest_v1,
        source_file_version="import-1",
    )

    assert graph.is_valid_for(source_file_hash=digest_v1, source_file_version="import-1")
    assert graph.status == FormulaStatus.COMPLETE
    source.write_bytes(b"v2")
    digest_v2 = sha256_file(source)
    assert digest_v1 != digest_v2
    assert not graph.is_valid_for(source_file_hash=digest_v2, source_file_version="import-1")
    assert graph.invalidate_if_changed(
        source_file_hash=digest_v2,
        source_file_version="import-1",
    )
    assert graph.invalidated is True
    assert graph.status == FormulaStatus.INCOMPLETE
    assert graph.invalidation_reason == "source_file_hash_or_version_changed"
    assert graph.impact("S!A1").status == FormulaStatus.INCOMPLETE


def test_file_based_invalidation_uses_current_sha256(tmp_path: Path) -> None:
    source = tmp_path / "source.xlsx"
    source.write_bytes(b"v1")
    graph = build_formula_graph(
        {"S!B1": "=A1"},
        source_file=str(source),
        source_file_hash=sha256_file(source),
    )

    assert not graph.invalidate_if_file_changed(source)
    source.write_bytes(b"v2")
    assert graph.invalidate_if_file_changed(source)
    assert graph.invalidated is True


def test_version_only_fingerprint_can_be_compared_without_a_silent_hash_claim() -> None:
    graph = build_formula_graph(
        {"S!B1": "=A1"},
        source_file="book.xlsx",
        source_file_version=3,
    )

    assert graph.is_valid_for(source_file_version=3)
    assert not graph.is_valid_for(source_file_version=4)
