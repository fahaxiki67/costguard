"""把 docs/QUICKSTART_zh-CN.md 转成 DMG 内可双击阅读的 RTF。

macOS 自带 textutil 完成 HTML→RTF；本脚本只做受限 Markdown 子集（标题/列表/
粗体/引用/段落）→ HTML 的确定性转换，不引入第三方依赖。
"""
from __future__ import annotations

import argparse
import html
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
QUICKSTART = REPO_ROOT / "docs" / "QUICKSTART_zh-CN.md"
OUT_NAME = "三分钟上手（先读我）.rtf"


def _md_inline(text: str) -> str:
    text = html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"`([^`]+)`", r"<tt>\1</tt>", text)
    return text


def md_to_html(md: str) -> str:
    lines = md.splitlines()
    out: list[str] = []
    in_list = False
    in_blockquote = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_md_inline(stripped[2:])}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        m = re.match(r"^(#{1,3})\s+(.*)$", stripped)
        if m:
            level = len(m.group(1)) + 1
            out.append(f"<h{level}>{_md_inline(m.group(2))}</h{level}>")
            continue
        if stripped.startswith(">"):
            if not in_blockquote:
                out.append("<blockquote>")
                in_blockquote = True
            out.append(f"<p>{_md_inline(stripped.lstrip('> '))}</p>")
            continue
        if in_blockquote:
            out.append("</blockquote>")
            in_blockquote = False
        if not stripped:
            continue
        out.append(f"<p>{_md_inline(stripped)}</p>")
    if in_list:
        out.append("</ul>")
    if in_blockquote:
        out.append("</blockquote>")
    style = (
        "body{font-family:'PingFang SC','Helvetica Neue',sans-serif;font-size:13px;"
        "line-height:1.55;margin:28px;color:#1a1a1a;} h1{font-size:20px;} h2{font-size:16px;"
        "border-bottom:1px solid #c9d4dc;padding-bottom:4px;} h3{font-size:14px;}"
        "blockquote{color:#5a6672;background:#f2f6f8;border-left:3px solid #2f8f7a;"
        "margin:8px 0;padding:6px 12px;} li{margin:2px 0;} tt{background:#eef1f3;}"
    )
    return f"<!doctype html><html><head><meta charset='utf-8'><style>{style}</style></head><body>{''.join(out)}</body></html>"


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 DMG 内的三分钟上手 RTF")
    parser.add_argument("--out", type=Path, required=True, help="输出目录")
    parser.add_argument("--source", type=Path, default=QUICKSTART)
    args = parser.parse_args()
    if not args.source.is_file():
        print(f"缺少源文档：{args.source}", file=sys.stderr)
        return 1
    html_text = md_to_html(args.source.read_text(encoding="utf-8"))
    tmp_html = args.out / "quickstart.html"
    tmp_html.write_text(html_text, encoding="utf-8")
    out_rtf = args.out / OUT_NAME
    subprocess.run(
        ["textutil", "-convert", "rtf", "-font", "PingFang SC", "-fontsize", "13",
         str(tmp_html), "-output", str(out_rtf)],
        check=True,
    )
    tmp_html.unlink()
    if not out_rtf.is_file() or out_rtf.stat().st_size < 1000:
        print(f"RTF 生成异常：{out_rtf}", file=sys.stderr)
        return 1
    print(f"已生成 {out_rtf}（{out_rtf.stat().st_size} bytes）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
