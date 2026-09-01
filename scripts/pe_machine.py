"""校验 PE 文件的机器类型（依赖无关，构建脚本用）。

用法：python scripts/pe_machine.py <pe文件> [期望machine的十六进制，如 8664]
machine 常量：0x8664=x64，0xAA64=ARM64，0x014C=x86。
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    expected = int(sys.argv[2], 16) if len(sys.argv) > 2 else None
    with open(path, "rb") as f:
        data = f.read(4096)
    if data[:2] != b"MZ":
        print(f"FAIL: {path} 不是 PE 文件（缺少 MZ 头）", file=sys.stderr)
        return 1
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    machine = struct.unpack_from("<H", data, e_lfanew + 4)[0]
    print(f"PE machine = 0x{machine:04X}")
    if expected is not None and machine != expected:
        print(f"FAIL: 期望 0x{expected:04X}，实际 0x{machine:04X}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
