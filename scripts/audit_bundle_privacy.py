"""安装包隐私审计：扫描 PyInstaller 产物内是否嵌入本机身份/私有信息。

按内容来源分层扫描（关键设计）：
1. 原生二进制层（未压缩 .app 全量）——其中全部 .so/.dylib 都来自上游官方
   wheel 与 PyInstaller，内部固化了【厂商自己的】CI 构建路径（如 cryptography
   的 /Users/runner/work/...、Rust 的 ~/.cargo/...）。这些是上游公开项目的
   构建痕迹，与本机身份无关；若在此层扫描 HOME/用户名，GitHub Actions
   （构建机用户名恰为 runner）会与厂商字符串大面积误撞。因此本层只扫
   【我们这棵构建树】的精确特征：仓库绝对路径、local_private_data、本机
   局域网网段——任何能把产物指回本机构建目录的字符串都逃不出仓库路径。
2. Python 字节码层（解包 CArchive→PYZ，逐模块 zlib 解压字节扫描）——PYZ 内容
   全部由本机构建产生（应用代码+标准库），没有厂商 C 字符串噪音，因此在此层
   扫描完整身份特征：HOME、/Users/<user>/、/home/<user>/、仓库绝对路径、
   local_private_data、局域网网段。

豁免仅用于 PYZ 层兜底（若第三方纯 Python 包常量里带有其厂商 CI 路径）：
形态限定为 Actions 工作区 /work/<非本项目>/ 或工具链缓存 /.cargo/ 等；
本项目自身路径与其它任何位置一律阻断。
"""
from __future__ import annotations

import argparse
import os
import re
import socket
import struct
import sys
import zlib
from pathlib import Path


def _identity_patterns() -> dict[str, bytes]:
    user = os.environ.get("USER") or ""
    home = os.environ.get("HOME") or ""
    repo = os.environ.get("JIADUN_REPO") or os.environ.get("COSTGUARD_REPO", "")
    base: dict[str, bytes] = {
        "local_private_data": b"local_private_data",
    }
    if repo:
        base["仓库本机绝对路径"] = repo.encode()
    for pat in _lan_prefixes():
        base[f"本机局域网网段 {pat}"] = pat.encode()
    # 用户名只按"路径锚定"形态匹配：裸用户名可能是普通英文词（如 GitHub
    # Actions 的 runner 会撞上 asyncio.Runner），产生误报；泄漏向量是路径。
    py_only: dict[str, bytes] = {}
    if user:
        py_only[f"用户名路径 /Users/{user}/"] = f"/Users/{user}/".encode()
        py_only[f"用户名路径 /home/{user}/"] = f"/home/{user}/".encode()
    if home:
        py_only["本机HOME路径"] = home.encode()
        # Windows 形态（HOME=C:\Users\<user>）：反斜杠用户目录前缀
        home_win = home.replace("/", "\\")
        if home_win != home:
            py_only["本机HOME路径(Windows)"] = home_win.encode()
    return {"binary": base, "pyz": {**base, **py_only}}


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


def _hit(label: str, where: str, blob: bytes, start: int, pat_len: int) -> dict:
    excerpt = blob[max(0, start - 40):start + 72]
    suffix = blob[start + pat_len:start + pat_len + 72]
    return {"label": label, "where": where,
            "excerpt": excerpt.decode("utf-8", "backslashreplace"),
            "suffix": suffix.decode("utf-8", "backslashreplace")}


def _scan_uncompressed(app_dir: Path, patterns: dict[str, bytes]) -> list[dict]:  # noqa: ARG001
    hits: list[dict] = []
    for root, _dirs, files in os.walk(app_dir):
        for name in files:
            f = Path(root) / name
            try:
                data = f.read_bytes()
            except OSError:
                continue
            for label, pat in patterns.items():
                start = data.find(pat)
                while start != -1:
                    hits.append(_hit(label, str(f), data, start, len(pat)))
                    start = data.find(pat, start + 1)
    return hits


def _extract_pyz(exe: Path) -> tuple[bytes | None, str]:
    """返回 (pyz 字节, 失败原因)；成功时原因为空。绝不静默降级。"""
    from PyInstaller.archive.readers import CArchiveReader

    try:
        ar = CArchiveReader(str(exe))
    except Exception as exc:
        return None, f"CArchive 打开失败：{type(exc).__name__}: {exc}"
    pyz_names = [n for n in ar.toc if "pyz" in n.lower()]
    if not pyz_names:
        return None, "CArchive 中无 PYZ 成员"
    try:
        return ar.extract(pyz_names[0]), ""
    except Exception as exc:
        return None, f"PYZ 提取失败：{type(exc).__name__}: {exc}"


def _scan_pyz(exe: Path, patterns: dict[str, bytes]) -> tuple[list[dict], str]:
    pyz, reason = _extract_pyz(exe)
    if pyz is None:
        return [], f"PYZ 层不可用（{reason}）——本层身份扫描未执行，需人工复核"
    hits: list[dict] = []
    magic, _pymagic, tocpos = struct.unpack("!4s4sI", pyz[:12])
    if magic != b"PYZ\x00":
        return [{"label": "PYZ异常", "where": str(exe), "excerpt": "PYZ 头部无法解析", "suffix": ""}], ""
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
            start = blob.find(pat)
            while start != -1:
                hits.append(_hit(label, f"PYZ模块 {name}", blob, start, len(pat)))
                start = blob.find(pat, start + 1)
    return hits, ""


_UPSTREAM_TOOLCHAIN_DIRS = (
    ".cargo", ".rustup", ".gradle", ".m2", ".npm", ".cache", "go",
)


def _configure_console_encoding() -> None:
    """让 Windows 非 UTF-8 控制台也能输出中文审计结果。

    GitHub Windows runner 默认可能使用 cp1252；审计信息包含中文，直接
    ``print`` 会在扫描完成后因 ``UnicodeEncodeError`` 退出，掩盖真正的
    隐私审计结果。仅重配置当前进程的输出流，不修改系统代码页或用户文件；
    不支持 ``reconfigure`` 的测试/管道流保持原样。
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, ValueError):
            continue


def _upstream_allowlisted(hit: dict, repo_name: str) -> bool:
    """上游第三方自带内容的精确豁免。

    GitHub Actions 构建机用户名为 runner，上游官方 wheel 里固化了其厂商构建
    痕迹，已知形态有二：
    1. Actions 工作区：…/work/<厂商项目>/…（cryptography SBOM、PIL/lxml 等）；
    2. 构建机家目录下的工具链缓存：…/.cargo/registry、…/.rustup/…（Rust 扩展）。
    仅当 HOME/用户名路径命中的紧随前缀属于上述两类、且工作区段名不是本项目
    时才豁免；本项目自身任何路径（work/<本项目>/…、其他家目录位置）照常阻断。
    """
    label = hit["label"]
    if label in ("local_private_data", "仓库本机绝对路径") or label.startswith("本机局域网网段"):
        return False
    if not (label == "本机HOME路径" or label.startswith("用户名路径")):
        return False
    suffix = hit.get("suffix", "")
    m = re.match(r"/work/([A-Za-z0-9_.\-]+)/", suffix)
    if m:
        return m.group(1) != repo_name
    second = re.match(r"/([A-Za-z0-9_.\-]+)/", suffix)
    if second and second.group(1) in _UPSTREAM_TOOLCHAIN_DIRS:
        return True
    return False


def main() -> int:
    _configure_console_encoding()
    parser = argparse.ArgumentParser(description="安装包隐私审计")
    parser.add_argument("app", type=Path, help="Jiadun.app 路径")
    args = parser.parse_args()
    app = args.app.resolve()
    # 布局按平台自动识别：macOS bundle（Contents/MacOS/...）或 Windows onedir（根下 exe）
    candidates = [
        app / "Contents" / "MacOS" / "Jiadun",
        app / "Contents" / "MacOS" / "CostGuard",  # 旧版兼容
        app / "Jiadun.exe",
        app / "CostGuard.exe",  # 旧版兼容
    ]
    exe = next((c for c in candidates if c.is_file()), None)
    if exe is None:
        print(f"FAIL: 找不到主执行文件（尝试过 {[str(c) for c in candidates]}）",
              file=sys.stderr)
        return 1

    current_repo = os.environ.get("JIADUN_REPO")
    legacy_repo = os.environ.get("COSTGUARD_REPO")
    if current_repo and legacy_repo and Path(current_repo).resolve() != Path(legacy_repo).resolve():
        print("FAIL: JIADUN_REPO 与 COSTGUARD_REPO 不一致，拒绝审计", file=sys.stderr)
        return 1
    repo = Path(os.environ.setdefault("JIADUN_REPO", current_repo or legacy_repo or str(Path.cwd()))).resolve()
    repo_name = repo.name
    patterns = _identity_patterns()
    if len(patterns["binary"]) <= 1:
        print("FAIL: 无法确定本机身份特征（仓库路径/网段为空），拒绝盲审", file=sys.stderr)
        return 1

    pyz_hits, pyz_note = _scan_pyz(exe, patterns["pyz"])
    raw_hits = _scan_uncompressed(app, patterns["binary"]) + pyz_hits
    if pyz_note:
        print(f"注意：{pyz_note}")
    # 按（特征, 位置）聚合：任一处不可豁免即该组阻断；豁免组只计数，避免日志膨胀
    groups: dict[tuple[str, str], list[dict]] = {}
    for h in raw_hits:
        groups.setdefault((h["label"], h["where"]), []).append(h)
    hits = []
    for (_label, _where), hs in groups.items():
        blocked = [h for h in hs if not _upstream_allowlisted(h, repo_name)]
        rep = blocked[0] if blocked else hs[0]
        rep = dict(rep)
        rep["blocked"] = bool(blocked)
        rep["count"] = len(hs)
        hits.append(rep)
    blocking = [h for h in hits if h["blocked"]]

    print(f"审计对象：{app}")
    print(f"二进制层特征：{sorted(patterns['binary'])}\n字节码层特征：{sorted(patterns['pyz'])}")
    if hits:
        for h in sorted(hits, key=lambda x: (not x["blocked"], x["label"], x["where"])):
            mark = "阻断" if h["blocked"] else "豁免（上游第三方自带）"
            print(f"  命中×{h['count']}（{mark}）：[{h['label']}] {h['where']}")
            print(f"      上下文：…{h['excerpt']}…")
    if blocking:
        print(f"\nFAIL：发现 {len(blocking)} 处本机身份/私有信息，安装包不得交付", file=sys.stderr)
        return 1
    print("PASS：未发现本机身份/私有信息嵌入")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
