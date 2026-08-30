"""生成 CostGuard 应用图标（src/costguard/resources/icon.icns）。

用 PySide6 离屏渲染 1024×1024 母版（纯几何图形，无文字/无字体依赖），
sips/iconutil 生成 macOS iconset。重新运行输出字节一致（同机确定性）。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_ICNS = REPO_ROOT / "src" / "costguard" / "resources" / "icon.icns"

ICONSET_SIZES = [16, 32, 128, 256, 512]


def _render_master(size: int = 1024) -> bytes:
    from PySide6.QtCore import QBuffer, QIODevice, QPointF, QRectF, Qt
    from PySide6.QtGui import (
        QBrush,
        QColor,
        QGuiApplication,
        QLinearGradient,
        QPainter,
        QPainterPath,
        QPen,
        QPixmap,
    )

    _app = QGuiApplication.instance() or QGuiApplication([])  # noqa: F841 — QPixmap 前置条件
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    s = size / 1024.0

    # 背景：圆角方块 + 深青渐变
    margin, radius = 64 * s, 180 * s
    bg = QRectF(margin, margin, size - 2 * margin, size - 2 * margin)
    grad = QLinearGradient(bg.topLeft(), bg.bottomRight())
    grad.setColorAt(0.0, QColor("#1d3f57"))
    grad.setColorAt(1.0, QColor("#2f8f7a"))
    p.setBrush(QBrush(grad))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(bg, radius, radius)

    # 白色上升柱状条（结算累计）
    bar_w, gap = 120 * s, 60 * s
    base_y = bg.bottom() - 150 * s
    heights = [220 * s, 330 * s, 450 * s]
    x0 = bg.left() + 150 * s
    for i, h in enumerate(heights):
        bar = QRectF(x0 + i * (bar_w + gap), base_y - h, bar_w, h)
        path = QPainterPath()
        path.addRoundedRect(bar, 28 * s, 28 * s)
        p.setBrush(QColor(255, 255, 255, 235))
        p.drawPath(path)

    # 右上角校核对勾（双向校核/人工复核）
    cx, cy, r = bg.right() - 250 * s, bg.top() + 250 * s, 150 * s
    p.setBrush(QColor("#ffffff"))
    p.drawEllipse(QPointF(cx, cy), r, r)
    pen = QPen(QColor("#2f8f7a"), 34 * s, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    check = QPainterPath()
    check.moveTo(cx - 62 * s, cy + 6 * s)
    check.lineTo(cx - 12 * s, cy + 56 * s)
    check.lineTo(cx + 66 * s, cy - 50 * s)
    p.drawPath(check)
    p.end()

    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    pm.save(buf, "PNG")
    return bytes(buf.data())


def main() -> int:
    if sys.platform != "darwin":
        print("仅支持 macOS（iconutil）", file=sys.stderr)
        return 1
    for tool in ("sips", "iconutil"):
        if shutil.which(tool) is None:
            print(f"缺少 {tool}", file=sys.stderr)
            return 1
    png = _render_master(1024)
    with tempfile.TemporaryDirectory() as td:
        iconset = Path(td) / "CostGuard.iconset"
        iconset.mkdir()
        master = Path(td) / "master.png"
        master.write_bytes(png)
        for size in ICONSET_SIZES:
            for name, target in ((f"icon_{size}x{size}.png", size),
                                 (f"icon_{size}x{size}@2x.png", size * 2)):
                out = iconset / name
                subprocess.run(["sips", "-z", str(target), str(target),
                                str(master), "--out", str(out)],
                               check=True, capture_output=True)
        OUT_ICNS.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["iconutil", "-c", "icns", str(iconset),
                        "-o", str(OUT_ICNS)], check=True)
    print(f"已生成 {OUT_ICNS}（{OUT_ICNS.stat().st_size} bytes）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
