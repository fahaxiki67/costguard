"""第二读取器隔离试验测试。"""

from pathlib import Path

from costguard.core.parsing.reader_trial import (
    ReaderSnapshot,
    SheetSnapshot,
    run_isolated_reader_trial,
)


def _reader(name: str, rows: int = 10):
    def read(path: Path) -> ReaderSnapshot:
        return ReaderSnapshot(
            reader_name=name,
            source_path=str(path),
            file_type=path.suffix,
            sheets=(SheetSnapshot(
                name="明细",
                visible=True,
                row_count=rows,
                date_count=1,
                null_count=0,
                merged_ranges=("A1:B1",),
                control_totals=("100.00",),
            ),),
        )
    return read


def test_reader_trial_is_closed_by_default():
    result = run_isolated_reader_trial("sample.xlsx", _reader("primary"), _reader("secondary"))
    assert result.status == "disabled"
    assert result.passed is False
    assert result.primary is None


def test_reader_trial_reports_difference_without_selecting_a_reader(tmp_path):
    source = tmp_path / "sample.xlsx"
    result = run_isolated_reader_trial(
        source,
        _reader("primary", rows=10),
        _reader("secondary", rows=11),
        enabled=True,
    )
    assert result.status == "complete"
    assert result.passed is False
    assert result.differences[0]["field"] == "sheet[0].row_count"
    assert result.differences[0]["primary"] == 10
    assert result.differences[0]["secondary"] == 11
    assert result.as_dict()["primary"]["reader_name"] == "primary"


def test_reader_trial_keeps_unsupported_formats_out_of_scope(tmp_path):
    result = run_isolated_reader_trial(
        tmp_path / "sample.ods",
        _reader("primary"),
        _reader("secondary"),
        enabled=True,
    )
    assert result.status == "incomplete"
    assert "ods" in result.limitations[0]
