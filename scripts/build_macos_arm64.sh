#!/usr/bin/env bash
# CostGuard macOS Apple Silicon 安装包构建（可重复、失败即非零退出）。
#
# 产物：dist/CostGuard-<version>-macos-arm64.dmg
#   - CostGuard.app（PyInstaller onedir，ad-hoc 签名，arm64 原生）
#   - Applications 快捷方式（拖入安装）
#   - 三分钟上手（先读我）.rtf
#   - 匿名演示数据/（examples/demo 合成数据副本）
#
# 纪律：
# - 只清理可再生输出（build/ dist/），绝不触碰用户工程资料；
# - 全量测试/lint/演示数据确定性/隐私审计都是硬门槛；
# - 无 Developer ID 时 ad-hoc 本地签名，不宣称公证或"无 Gatekeeper 提示"。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

log()  { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
fail() { printf '\033[1;31mFAIL: %s\033[0m\n' "$*" >&2; exit 1; }

SKIP_CHECKS=0
for arg in "$@"; do
  case "$arg" in
    --skip-checks) SKIP_CHECKS=1 ;;
    *) fail "未知参数：${arg}（支持 --skip-checks）" ;;
  esac
done

# ---- 1. 机器与 Python 版本 ----
log "环境检查"
[[ "$(uname -s)" == "Darwin" ]] || fail "仅支持 macOS，当前 $(uname -s)"
[[ "$(uname -m)" == "arm64" ]] || fail "仅支持 Apple Silicon (arm64)，当前 $(uname -m)"
PYV="$(uv run python -c 'import platform; print(platform.python_version())')"
[[ "$PYV" == 3.12.* ]] || fail "需要 Python 3.12（ADR-001），当前 $PYV"
echo "  macOS $(sw_vers -productVersion) / arm64 / Python $PYV"

# ---- 2. 质量门槛 ----
if [[ $SKIP_CHECKS -eq 0 ]]; then
  log "质量门槛：ruff + 全量测试"
  uv run ruff check src scripts tests || fail "ruff 未通过"
  uv run python -m pytest tests/ || fail "全量测试未通过"
else
  log "跳过质量门槛（--skip-checks）"
fi

# ---- 3. 演示数据确定性 ----
log "演示数据确定性校验（安装包内置演示必须与仓库字节一致）"
uv run python scripts/generate_demo_data.py --check || \
  fail "演示数据不确定：请先运行 scripts/generate_demo_data.py 重新生成并提交"

# ---- 4. 清理（仅本次可再生输出）----
log "清理 build/ dist/（仅可再生输出；不触碰用户工程资料）"
rm -rf build dist
mkdir -p build dist

# ---- 5. 版本号与最低 macOS 版本 ----
VERSION="$(uv run python -c 'import tomllib; print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])')"
[[ "$VERSION" != "0.0.0" ]] || fail "版本号解析失败"
MINOS_MEASURED="$(uv run python - <<'PY'
import pathlib, subprocess

def key(v):
    return tuple(int(x) for x in v.split("."))

best = "11.0"
base = pathlib.Path(".venv/lib/python3.12/site-packages/PySide6")
if not base.is_dir():
    raise SystemExit("PySide6 not found in .venv")
for f in base.rglob("*"):
    if f.suffix not in (".so", ".dylib") or not f.is_file():
        continue
    out = subprocess.run(["otool", "-l", str(f)], capture_output=True, text=True).stdout
    for i, line in enumerate(out.splitlines()):
        s = line.strip()
        if s.startswith("minos"):
            v = s.split()[1]
            if key(v) > key(best):
                best = v
            break
print(best)
PY
)"
[[ -n "$MINOS_MEASURED" ]] || fail "最低 macOS 版本实测失败"
log "版本 ${VERSION}；实测最低 macOS ${MINOS_MEASURED}（取 PySide6 二进制 minos 最大值）"
export COSTGUARD_VERSION="$VERSION"
export COSTGUARD_MIN_MACOS="$MINOS_MEASURED"

# ---- 6. 应用图标 ----
if [[ ! -f src/costguard/resources/icon.icns ]]; then
  log "生成应用图标"
  uv run python scripts/generate_icon.py || fail "图标生成失败"
fi

# ---- 7. PyInstaller 构建 ----
log "PyInstaller 构建 CostGuard.app"
uv run pyinstaller --noconfirm --clean --distpath dist --workpath build \
  src/costguard/platform/packaging/macos_arm64.spec || fail "PyInstaller 构建失败"
APP="dist/CostGuard.app"
[[ -d "$APP" ]] || fail "未找到 $APP"

# ---- 8. 架构校验 ----
ARCHS="$(lipo -archs "$APP/Contents/MacOS/CostGuard")"
echo "  主执行文件架构：$ARCHS"
[[ "$ARCHS" == *arm64* ]] || fail "主执行文件不含 arm64：$ARCHS"
[[ "$ARCHS" != *x86_64* ]] || fail "混入 x86_64（本脚本仅产 arm64 原生包）：$ARCHS"
/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "$APP/Contents/Info.plist" | grep -qx "$VERSION" || \
  fail "Info.plist 版本与 pyproject 不一致"
/usr/libexec/PlistBuddy -c "Print :CFBundleIdentifier" "$APP/Contents/Info.plist" | grep -qx "io.github.fahaxiki67.costguard" || \
  fail "Bundle Identifier 不正确"

# ---- 9. codesign（ad-hoc）+ 自检 ----
log "ad-hoc 签名与自检"
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || fail "ad-hoc 签名失败"
codesign --verify --deep --strict "$APP" || fail "codesign 自检失败"
codesign -dv "$APP" 2>&1 | sed 's/^/  /' | head -4 || true

# ---- 10. 隐私审计（硬门槛）----
log "安装包隐私审计（本机用户名/HOME/仓库路径/局域网网段/private_data）"
uv run python scripts/audit_bundle_privacy.py "$APP" || fail "隐私审计未通过"

# ---- 11. DMG 组装 ----
log "组装 DMG 内容"
STAGING="build/dmg-staging"
mkdir -p "$STAGING"
cp -R "$APP" "$STAGING/CostGuard.app"
ln -s /Applications "$STAGING/Applications"
mkdir -p "$STAGING/匿名演示数据"
cp examples/demo/*.xlsx examples/demo/*.docx \
   examples/demo/README_zh-CN.md examples/demo/manifest.json examples/demo/SHA256SUMS \
   "$STAGING/匿名演示数据/"
uv run python scripts/make_dmg_readme.py --out "$STAGING" || fail "三分钟上手文档生成失败"
ls "$STAGING" | sed 's/^/  - /'

DMG="dist/CostGuard-${VERSION}-macos-arm64.dmg"
[[ -e "$DMG" ]] && rm -f "$DMG"
log "hdiutil 打包 $DMG"
hdiutil create -volname "CostGuard" -srcfolder "$STAGING" -format UDZO -ov "$DMG" >/dev/null || \
  fail "hdiutil 创建失败"

# ---- 12. DMG 挂载自检 ----
log "DMG 挂载自检"
MP="build/dmg-mount"
mkdir -p "$MP"
hdiutil attach "$DMG" -readonly -nobrowse -mountpoint "$MP" >/dev/null || fail "DMG 挂载失败"
OK=1
[[ -d "$MP/CostGuard.app" ]] || { echo "缺少 CostGuard.app"; OK=0; }
[[ -L "$MP/Applications" ]] || { echo "缺少 Applications 快捷方式"; OK=0; }
[[ -f "$MP/三分钟上手（先读我）.rtf" ]] || { echo "缺少三分钟上手文档"; OK=0; }
[[ -d "$MP/匿名演示数据" ]] || { echo "缺少匿名演示数据目录"; OK=0; }
[[ "$(ls "$MP/匿名演示数据" | wc -l | tr -d ' ')" -ge 7 ]] || { echo "演示数据不完整"; OK=0; }
codesign --verify --deep --strict "$MP/CostGuard.app" || { echo "DMG 内 app 签名校验失败"; OK=0; }
hdiutil detach "$MP" -quiet >/dev/null 2>&1 || true
[[ $OK -eq 1 ]] || fail "DMG 内容自检失败"

# ---- 13. SHA-256 与产物信息 ----
SHA="$(shasum -a 256 "$DMG" | awk '{print $1}')"
SIZE_H="$(du -h "$DMG" | awk '{print $1}')"
SIZE_B="$(stat -f%z "$DMG")"
echo "$SHA  $(basename "$DMG")" > dist/SHA256SUMS.txt
printf '\n\033[1;32m构建完成\033[0m\n'
printf '  DMG     : %s/%s\n' "$(pwd)" "$DMG"
printf '  大小    : %s (%s bytes)\n' "$SIZE_H" "$SIZE_B"
printf '  SHA-256 : %s\n' "$SHA"
printf '  版本    : %s（最低 macOS %s，ad-hoc 本地签名，未公证）\n' "$VERSION" "$MINOS_MEASURED"
