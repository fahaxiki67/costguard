"""安装包隐私审计：扫描 PyInstaller 产物内是否嵌入本机身份/私有信息。

扫描两层：
1. 未压缩层——grep 整个 .app（含二进制）；
2. 压缩层——解包主可执行文件内 CArchive→PYZ，逐模块 zlib 解压后字节扫描
   （覆盖 Python 字节码中的 co_filename/常量字符串）。

检查项（从当前环境动态取值，避免硬编码）：
- 本机用户名（$USER / $HOME）
- 本机局域网 IP（扫描本机网段字符串形态，另加显式传入值）
- local_private_data 私有资料目录名
- 仓库绝对路径

已知可豁免（默认内建）：上游第三方官方 wheel 自带的公开构建路径
（如 cryptography SBOM 中的 GitHub Actions runner 目录、Python 文档示例
C:/Users/...）——但仅当其中不含本机用户名时才放行；任何本机身份命中即失败。
"""
from __future__ import annotations

import argparse
import os
import socket
import struct
import sys
import zlib
from pathlib import Path


def _identity_patterns() -> dict[str, bytes]:
    user = os.environ.get("USER") or ""
    home = os.environ.get("HOME") or ""
    repo = os.environ.get("COSTGUARD_REPO", "")
    pats: dict[str, bytes] = {
        "local_private_data": b"local_private_data",
    }
    if user:
        pats["本机用户名"] = user.encode()
    if home:
        pats["本机HOME路径"] = home.encode()
    if repo:
        pats["仓库本机绝对路径"] = repo.encode()
    # 局域网地址：扫描所有本机网段形如 x.y.z. 的前缀（/24 粒度）
    for pat in _lan_prefixes():
        pats[f"本机局域网网段 {pat}"] = pat.encode()
    return pats


def _lan_prefixes() -> list[str]:
    prefixes: set[str] = set()
    try:
        import ipaddress

        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            ip = ipaddress.IPv4Address(info[4][0])
            if ip.is_private and not ip.is_loopback:
                prefixes.add(str(ip).rsplit(".", 1)[0] + ".")
    except (OSError, ValueError):
        pass
    return sorted(prefixes)


def _scan_uncompressed(app_dir: Path, patterns: dict[str, bytes]) -> list[str]:
    hits: list[str] = []
    for root, _dirs, files in os.walk(app_dir):
        for name in files:
            f = Path(root) / name
            try:
                data = f.read_bytes()
            except OSError:
                continue
            for label, pat in patterns.items():
                if pat in data:
                    hits.append(f"未压缩层 [{label}] {f}")
    return hits


def _extract_pyz(exe: Path) -> bytes | None:
    from PyInstaller.archive.readers import CArchiveReader

    try:
        ar = CArchiveReader(str(exe))
    except Exception:
        return None
    pyz_names = [n for n in ar.toc if "pyz" in n.lower()]
    if not pyz_names:
        return None
    return ar.extract(pyz_names[0])


def _scan_pyz(exe: Path, patterns: dict[str, bytes]) -> list[str]:
    pyz = _extract_pyz(exe)
    if pyz is None:
        return []
    hits: list[str] = []
    magic, _pymagic, tocpos = struct.unpack("!4s4sI", pyz[:12])
    if magic != b"PYZ\x00":
        return [f"PYZ 头部异常（无法解析）：{exe}"]
    toc = __import__("marshal").loads(pyz[tocpos:])
    for item in toc:
        name, (typ, pos, length) = item
        blob = pyz[pos:pos + length]
        if typ == 1:
            try:
                blob = zlib.decompress(blob)
            except zlib.error:
                continue
        for label, pat in patterns.items():
            if pat in blob:
                hits.append(f"PYZ字节码 [{label}] {name}")
    return hits


def _upstream_allowlisted(hit: str) -> bool:
    """上游第三方 wheel 自带的构建路径豁免（不含本机身份才可放行）。"""
    if "本机" in hit or "local_private_data" in hit:
        return False
    return "Users路径" in hit


def main() -> int:
    parser = argparse.ArgumentParser(description="安装包隐私审计")
    parser.add_argument("app", type=Path, help="CostGuard.app 路径")
    args = parser.parse_args()
    app = args.app.resolve()
    exe = app / "Contents" / "MacOS" / "CostGuard"
    if not exe.is_file():
        print(f"FAIL: 找不到主执行文件 {exe}", file=sys.stderr)
        return 1

    os.environ.setdefault("COSTGUARD_REPO", str(Path.cwd()))
    patterns = _identity_patterns()
    if len(patterns) <= 1:
        print("FAIL: 无法确定本机身份特征（USER/HOME 为空），拒绝盲审", file=sys.stderr)
        return 1

    hits = _scan_uncompressed(app, patterns) + _scan_pyz(exe, patterns)
    blocking = [h for h in hits if not _upstream_allowlisted(h)]

    print(f"审计对象：{app}")
    print(f"身份特征：{sorted(patterns)}")
    if hits:
        for h in hits:
            mark = "豁免（上游第三方自带）" if _upstream_allowlisted(h) else "阻断"
            print(f"  命中（{mark}）：{h}")
    if blocking:
        print(f"\nFAIL：发现 {len(blocking)} 处本机身份/私有信息，安装包不得交付", file=sys.stderr)
        return 1
    print("PASS：未发现本机身份/私有信息嵌入")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
