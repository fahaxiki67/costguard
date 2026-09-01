"""资料文件选择与拖拽的轻量 UI 边界。

这里仅负责把用户选中的文件/文件夹转换为确定的、可导入的路径列表；
实际复制、解析和落库仍由 core 导入器完成。目录扫描不跟随符号链接，
避免拖入一个包含循环链接的资料目录时卡死。
"""
from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout

SETTLEMENT_SUFFIXES = frozenset({".xlsx", ".xlsm", ".xls", ".csv"})
CONTRACT_SUFFIXES = frozenset({".docx", ".pdf", ".txt"})
SUPPORTED_SUFFIXES = SETTLEMENT_SUFFIXES | CONTRACT_SUFFIXES


@dataclass(frozen=True)
class ImportSelection:
    """一次选择中可导入文件与明确跳过的非支持文件。"""

    files: tuple[Path, ...]
    skipped: tuple[Path, ...]
    # 与 ``skipped`` 同步的（路径、原因）明细，供 UI 解释权限、链接和类型
    # 边界；保留 ``skipped`` 这个简单路径列表以兼容现有调用方。
    skipped_reasons: tuple[tuple[Path, str], ...] = ()


def classify_import_file(path: str | Path) -> str | None:
    """返回 ``settlement``/``contract``，不支持时返回 ``None``。"""
    suffix = Path(path).suffix.lower()
    if suffix in SETTLEMENT_SUFFIXES:
        return "settlement"
    if suffix in CONTRACT_SUFFIXES:
        return "contract"
    return None


def _iter_directory_files(
    root: Path, skipped_reasons: list[tuple[Path, str]]
) -> Iterable[Path]:
    """递归枚举目录内普通文件，不跟随目录或文件符号链接。"""
    def onerror(error: OSError) -> None:
        target = Path(getattr(error, "filename", None) or root)
        skipped_reasons.append((target, "目录无法读取"))

    for current, dirs, files in os.walk(root, followlinks=False, onerror=onerror):
        # Finder/Office 临时文件不应被误报为资料，也不应进入导入器。
        kept_dirs: list[str] = []
        for name in sorted(dirs):
            if name.startswith("."):
                continue
            candidate = Path(current) / name
            try:
                if candidate.is_symlink():
                    skipped_reasons.append((candidate, "符号链接目录未导入"))
                    continue
            except OSError:
                skipped_reasons.append((candidate, "目录无法读取"))
                continue
            kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in sorted(files):
            if name.startswith(".") or name.startswith("~$"):
                continue
            path = Path(current) / name
            try:
                if path.is_symlink():
                    skipped_reasons.append((path, "符号链接文件未导入"))
                elif path.is_file():
                    yield path
                else:
                    skipped_reasons.append((path, "文件无法读取"))
            except OSError:
                skipped_reasons.append((path, "文件无法读取"))


def scan_import_paths(paths: Iterable[str | Path]) -> ImportSelection:
    """扫描用户选中的文件或文件夹，排序、去重并保留跳过项。

    不会修改任何输入文件；目录中的不支持类型会列入 ``skipped``，由 UI
    告知用户，而不是静默把它们当成导入成功。
    """
    candidates: list[Path] = []
    skipped_reasons: list[tuple[Path, str]] = []
    for raw in paths:
        raw_text = "" if raw is None else str(raw)
        if not raw_text.strip():
            # Path("") 会被解释成当前目录；无效拖拽/调用参数必须在构造
            # Path 之前拦截，避免把工作目录误当成用户选择的资料目录。
            skipped_reasons.append((Path("<空路径>"), "空路径未导入"))
            continue
        path = Path(raw_text).expanduser()
        try:
            if path.is_symlink():
                skipped_reasons.append((path, "符号链接路径未导入"))
            elif path.is_dir():
                candidates.extend(_iter_directory_files(path, skipped_reasons))
            elif path.is_file():
                candidates.append(path)
            else:
                skipped_reasons.append((path, "路径不存在或无法读取"))
        except OSError:
            skipped_reasons.append((path, "路径无法读取"))

    files: list[Path] = []
    skipped: list[Path] = []
    seen: set[str] = set()
    for path in sorted(candidates, key=lambda item: os.path.normcase(str(item))):
        key = os.path.normcase(os.path.realpath(str(path)))
        if key in seen:
            continue
        seen.add(key)
        if classify_import_file(path) is None:
            skipped.append(path)
            skipped_reasons.append((path, "不支持的文件类型"))
        else:
            files.append(path)

    # 路径去重只针对显示项，不用 realpath：如果一个坏链接和普通路径同指向
    # 一个目标，仍要让用户看到自己明确选过的链接边界。
    unique_skipped: list[Path] = []
    unique_details: list[tuple[Path, str]] = []
    seen_skipped: set[str] = set()
    for path, reason in sorted(
        skipped_reasons, key=lambda item: os.path.normcase(str(item[0]))
    ):
        key = os.path.normcase(os.path.normpath(str(path)))
        if key in seen_skipped:
            continue
        seen_skipped.add(key)
        unique_skipped.append(path)
        unique_details.append((path, reason))
    return ImportSelection(tuple(files), tuple(unique_skipped), tuple(unique_details))


def preferred_project_name(paths: Iterable[str | Path], files: Iterable[Path] = ()) -> str:
    """为从资料创建项目提供可编辑的建议名称。"""
    selected = [Path(item).expanduser() for item in paths]
    for path in selected:
        if path.is_dir() and path.name:
            return path.name
    file_list = list(files)
    if len(file_list) == 1 and file_list[0].stem:
        return file_list[0].stem
    if file_list and file_list[0].parent.name:
        return file_list[0].parent.name
    return "新建工程项目"


class FileDropZone(QFrame):
    """可接收 Finder/资源管理器文件与文件夹拖入的提示区域。"""

    paths_dropped = Signal(object)

    def __init__(self, text: str = "将结算或合同资料拖到这里", parent=None):
        super().__init__(parent)
        self.setObjectName("fileDropZone")
        self.setAcceptDrops(True)
        self.setMinimumHeight(64)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        label = QLabel(text)
        label.setObjectName("fileDropZoneLabel")
        label.setAlignment(Qt.AlignCenter)
        label.setWordWrap(True)
        layout.addWidget(label)

    @staticmethod
    def paths_from_mime(mime) -> list[Path]:
        """从 Qt 拖放数据中提取本地文件路径。"""
        if not mime or not mime.hasUrls():
            return []
        paths: list[Path] = []
        for url in mime.urls():
            if not url.isLocalFile():
                continue
            local_file = url.toLocalFile()
            if not local_file or not local_file.strip():
                continue
            paths.append(Path(local_file))
        return paths

    def dragEnterEvent(self, event):  # noqa: N802 - Qt override
        if self.paths_from_mime(event.mimeData()):
            self.setProperty("dragActive", True)
            self.style().unpolish(self)
            self.style().polish(self)
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):  # noqa: N802 - Qt override
        if self.paths_from_mime(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):  # noqa: N802 - Qt override
        paths = self.paths_from_mime(event.mimeData())
        if not paths:
            event.ignore()
            return
        self.setProperty("dragActive", False)
        self.style().unpolish(self)
        self.style().polish(self)
        self.paths_dropped.emit(paths)
        event.acceptProposedAction()

    def dragLeaveEvent(self, event):  # noqa: N802 - Qt override
        self.setProperty("dragActive", False)
        self.style().unpolish(self)
        self.style().polish(self)
        event.accept()


__all__ = [
    "CONTRACT_SUFFIXES",
    "FileDropZone",
    "ImportSelection",
    "SETTLEMENT_SUFFIXES",
    "SUPPORTED_SUFFIXES",
    "classify_import_file",
    "preferred_project_name",
    "scan_import_paths",
]
