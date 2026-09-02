# Jiadun Windows x64 构建脚本（可重复、失败即非零退出）。
# 与 scripts/build_macos_arm64.sh 同一门禁纪律：
#   环境(arch/Python3.12) → ruff+全量测试 → 演示数据确定性 → 只清理可再生输出
#   → PyInstaller 构建 → PE 架构校验 → 隐私审计 → 便携 zip + Inno 安装器 → SHA256
# 用法：powershell -ExecutionPolicy Bypass -File scripts/build_windows_x64.ps1 [-SkipChecks]

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot

function Fail([string]$msg) {
    Write-Host "FAIL: $msg" -ForegroundColor Red
    exit 1
}
function Step([string]$msg) {
    Write-Host ""
    Write-Host "== $msg ==" -ForegroundColor Cyan
}

$SkipChecks = $false
if ($args -contains "--skip-checks" -or $args -contains "-SkipChecks") { $SkipChecks = $true }

# ---- 1. 环境 ----
Step "环境检查"
if ($env:OS -ne "Windows_NT") { Fail "本脚本仅支持 Windows（$env:OS 缺失或非 Windows_NT）" }
$arch = $env:PROCESSOR_ARCHITECTURE
if ($arch -ne "AMD64") { Fail "仅支持 x64 (AMD64)，当前 $arch" }
$pyv = & uv run python -c "import platform; print(platform.python_version())"
if ($LASTEXITCODE -ne 0) { Fail "uv run python 失败" }
if (-not $pyv.StartsWith("3.12")) { Fail "需要 Python 3.12（ADR-001），当前 $pyv" }
Write-Host "  Windows x64 / Python $pyv"

# ---- 2. 质量门槛 ----
if (-not $SkipChecks) {
    Step "质量门槛：ruff + 全量测试"
    & uv run ruff check src scripts tests
    if ($LASTEXITCODE -ne 0) { Fail "ruff 未通过" }
    $env:QT_QPA_PLATFORM = "offscreen"
    & uv run python -m pytest tests/
    if ($LASTEXITCODE -ne 0) { Fail "全量测试未通过" }
} else {
    Step "跳过质量门槛（--skip-checks）"
}

# ---- 3. 演示数据确定性 ----
Step "演示数据确定性校验"
& uv run python scripts/generate_demo_data.py --check
if ($LASTEXITCODE -ne 0) { Fail "演示数据不确定（先运行 scripts/generate_demo_data.py 并提交）" }

# ---- 4. 只清理可再生输出 ----
Step "清理 build/ dist/（仅可再生输出；不触碰用户工程资料）"
foreach ($d in @("build", "dist")) {
    if (Test-Path $d) { Remove-Item -Recurse -Force $d }
}
New-Item -ItemType Directory -Force -Path build, dist | Out-Null

# ---- 5. 版本号 ----
$Version = & uv run python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])"
if (-not $Version -or $Version -eq "0.0.0") { Fail "版本号解析失败" }
$env:JIADUN_VERSION = $Version
Step "版本 $Version"

# ---- 6. 图标 ----
if (-not (Test-Path "src/jiadun/resources/icon.ico")) { Fail "缺少 icon.ico（本机先运行 scripts/generate_icon.py 或改用仓库内已提交图标）" }

# ---- 7. PyInstaller ----
Step "PyInstaller 构建 Jiadun"
& uv run pyinstaller --noconfirm --clean --distpath dist --workpath build src/jiadun/platform/packaging/windows_x64.spec
if ($LASTEXITCODE -ne 0) { Fail "PyInstaller 构建失败" }
$App = "dist/Jiadun"
if (-not (Test-Path "$App/Jiadun.exe")) { Fail "未找到 $App/Jiadun.exe" }

# ---- 8. PE 架构校验（主 exe 必须是 x64）----
Step "PE 架构校验"
& uv run python scripts/pe_machine.py "dist/Jiadun/Jiadun.exe" "8664"
if ($LASTEXITCODE -ne 0) { Fail "Jiadun.exe 不是 x64 (0x8664)" }

# ---- 9. 隐私审计 ----
Step "安装包隐私审计"
& uv run python scripts/audit_bundle_privacy.py "$App"
if ($LASTEXITCODE -ne 0) { Fail "隐私审计未通过" }

# ---- 10. 便携 zip ----
Step "打包便携版 zip"
$Portable = "dist/Jiadun-$Version-windows-x64-portable"
if (Test-Path $Portable) { Remove-Item -Recurse -Force $Portable }
Copy-Item -Recurse "$App" $Portable
Compress-Archive -Path $Portable -DestinationPath "dist/Jiadun-$Version-windows-x64-portable.zip" -Force
if ($LASTEXITCODE -ne 0) { Fail "便携 zip 压缩失败" }

# ---- 11. Inno Setup 安装器 ----
Step "Inno Setup 编译安装器"
$isccPath = $null
foreach ($cand in @("C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
                     "C:\Program Files\Inno Setup 6\ISCC.exe")) {
    if (Test-Path $cand) { $isccPath = $cand; break }
}
if (-not $isccPath) {
    $cmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($cmd) { $isccPath = $cmd.Source }
}
if (-not $isccPath) { Fail "未找到 Inno Setup 6（ISCC.exe）" }
Write-Host "  ISCC: $isccPath"
& $isccPath "src/jiadun/platform/packaging/windows_x64.iss"
if ($LASTEXITCODE -ne 0) { Fail "Inno Setup 编译失败" }
$Setup = "dist/installer/Jiadun-$Version-windows-x64-setup.exe"
if (-not (Test-Path $Setup)) { Fail "未找到安装器 $Setup" }

# ---- 11.5 代码签名（正式发布门槛；提供 JIADUN_SIGN_PFX 时启用）----
$SignPfx = $env:JIADUN_SIGN_PFX
if ($SignPfx) {
    Step "代码签名（SHA256 + 时间戳）"
    $signtool = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if (-not $signtool) {
        $kitRoot = "C:\Program Files (x86)\Windows Kits\10\bin"
        if (Test-Path $kitRoot) {
            $cand = Get-ChildItem $kitRoot -Directory | Sort-Object Name -Descending |
                ForEach-Object { Join-Path $_.FullName "x64\signtool.exe" } |
                Where-Object { Test-Path $_ } | Select-Object -First 1
            if ($cand) { $signtool = Get-Item $cand }
        }
    }
    if (-not $signtool) { Fail "未找到 signtool.exe（请安装 Windows SDK 或加入 PATH）" }
    if (-not $env:JIADUN_SIGN_PFX_PASSWORD) { Fail "设置了 JIADUN_SIGN_PFX 但缺少 JIADUN_SIGN_PFX_PASSWORD" }
    Write-Host "  signtool: $($signtool.FullName)"
    & $signtool.FullName sign /f $SignPfx /p $env:JIADUN_SIGN_PFX_PASSWORD /fd SHA256 `
        /tr http://timestamp.digicert.com /td SHA256 "$App\Jiadun.exe" $Setup
    if ($LASTEXITCODE -ne 0) { Fail "签名失败" }
    & $signtool.FullName verify /pa "$App\Jiadun.exe" $Setup
    if ($LASTEXITCODE -ne 0) { Fail "签名验证失败" }
    Write-Host "  签名验证通过（含便携版主程序与安装器）"
} else {
    Step "代码签名：未设置 JIADUN_SIGN_PFX，跳过（正式发布必须签名，清单中将记为 PENDING）"
}

# ---- 11.6 EXE 版本资源校验 ----
Step "EXE VERSIONINFO 校验"
$vi = (Get-Item "$App\Jiadun.exe").VersionInfo
if ($vi.ProductVersion -notlike "*$Version*") { Fail "ProductVersion=$($vi.ProductVersion) 与 $Version 不符" }
if (-not $vi.ProductName) { Fail "ProductName 缺失" }
if (-not $vi.CompanyName) { Fail "CompanyName(Publisher) 缺失" }
Write-Host ("  Product={0} | Version={1} | Company={2}" -f $vi.ProductName, $vi.ProductVersion, $vi.CompanyName)

# ---- 12. SHA256 ----
Step "SHA256 汇总"
$artifacts = @("dist/Jiadun-$Version-windows-x64-portable.zip", $Setup)
$lines = foreach ($a in $artifacts) {
    $h = (Get-FileHash $a -Algorithm SHA256).Hash.ToLower()
    "$h  $(Split-Path $a -Leaf)"
}
$lines | Set-Content -Encoding ascii "dist/SHA256SUMS.txt"

Write-Host ""
Write-Host "构建完成" -ForegroundColor Green
foreach ($a in $artifacts) {
    $size = (Get-Item $a).Length
    $h = (Get-FileHash $a -Algorithm SHA256).Hash.ToLower()
    Write-Host ("  {0}`n    大小: {1} bytes`n    SHA-256: {2}" -f (Resolve-Path $a).Path, $size, $h)
}
Write-Host ("  SHA256SUMS: {0}/dist/SHA256SUMS.txt" -f $RepoRoot)
