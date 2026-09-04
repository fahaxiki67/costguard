"""真实工程资料元数据盘点（任务书任务 A1）。

纪律：
- 只读取元数据（文件名/扩展名/大小/修改时间/相对目录），不打开文件正文；
- 不跟随符号链接；不对原件做任何写操作；
- 只处理用户显式指定的目录，绝不全盘扫描；
- 输出 CSV 写入 local_private_data/（gitignored），真实文件名不进 Git。

用法：
    uv run python scripts/inventory_real_corpus.py --root "D:\\某项目资料" \
        [--out local_private_data/corpus_inventory]

输出：inventory_<目录名>_<时间戳>.csv（UTF-8-SIG，Excel 可直接打开）。
盘点结果仅用于人工挑选进入 Real Corpus / Golden Case 的候选文件。
"""
from __future__ import annotations

import argparse
import csv
import datetime
import sys
from pathlib import Path

HEADER = ["relative_path", "file_name", "extension", "size_bytes", "modified_at", "sha256_pending"]


def inventory(root: Path) -> list[dict]:
    rows: list[dict] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        stat = path.stat()
        rows.append(
            {
                "relative_path": path.relative_to(root).as_posix(),
                "file_name": path.name,
                "extension": path.suffix.lower().lstrip("."),
                "size_bytes": stat.st_size,
                "modified_at": datetime.datetime.fromtimestamp(
                    stat.st_mtime, datetime.UTC
                ).isoformat(timespec="seconds"),
                # 按任务书 A1：盘点阶段不读正文，SHA-256 留待文件进入语料库时计算
                "sha256_pending": "",
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="真实资料元数据盘点（只读，不打开正文）")
    parser.add_argument("--root", type=Path, required=True, help="用户显式指定的资料目录")
    parser.add_argument(
        "--out", type=Path, default=Path("local_private_data/corpus_inventory"),
        help="输出目录（默认 local_private_data/corpus_inventory，不进 Git）",
    )
    parser.add_argument(
        "--min-size", type=int, default=0, help="忽略小于该字节数的文件（如 0=全部）",
    )
    args = parser.parse_args()

    root = args.root.resolve()
    if not root.is_dir():
        print(f"FAIL: 目录不存在：{root}", file=sys.stderr)
        return 1

    rows = [r for r in inventory(root) if r["size_bytes"] >= args.min_size]
    args.out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_file = args.out / f"inventory_{root.name}_{stamp}.csv"
    with open(out_file, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=HEADER)
        writer.writeheader()
        writer.writerows(rows)

    by_ext: dict[str, int] = {}
    for r in rows:
        by_ext[r["extension"] or "(无扩展名)"] = by_ext.get(r["extension"] or "(无扩展名)", 0) + 1
    print(f"盘点完成：{len(rows)} 个文件 → {out_file}")
    for ext, count in sorted(by_ext.items(), key=lambda kv: -kv[1]):
        print(f"  .{ext}: {count}")
    print("下一步：人工从清单中挑选进入 Real Corpus / Golden Case 的文件（A1：盘点不等于入选）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
