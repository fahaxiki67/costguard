; CostGuard Windows x64 安装器脚本（Inno Setup 6）
; 由 scripts/build_windows_x64.ps1 调用；版本号经环境变量注入。
; 纪律：
; - 只安装程序本体到 Program Files；用户工程数据在 Documents\CostGuardProjects，
;   卸载绝不触碰（UninstallFilesDir 只覆盖程序目录，无任何用户数据 Clean 步骤）；
; - 无后台常驻、无自动更新、无驱动；请求最低权限（lowest）；
; - 未签名安装包：SmartScreen 可能提示，文档如实说明，不承诺无提示。

#define CostGuardName "CostGuard"
#define CostGuardVersion GetEnv("COSTGUARD_VERSION")
#if CostGuardVersion == ""
#define CostGuardVersion "0.0.0"
#endif

[Setup]
AppId={{8F1E2A64-9C3B-4B7D-9E5F-C0DEC0DEC005}
AppName={#CostGuardName}
AppVersion={#CostGuardVersion}
AppVerName={#CostGuardName} {#CostGuardVersion}
AppPublisher=CostGuard project
AppPublisherURL=https://github.com/fahaxiki67/costguard
DefaultDirName={autopf}\{#CostGuardName}
DefaultGroupName={#CostGuardName}
DisableProgramGroupPage=yes
OutputDir=..\..\dist\installer
OutputBaseFilename=CostGuard-{#CostGuardVersion}-windows-x64-setup
Compression=lzma2/max
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
UninstallDisplayIcon={app}\CostGuard.exe
SetupIconFile=..\..\src\costguard\resources\icon.ico
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

; 中文向导若镜像自带则启用，否则回退英文默认（不同 runner 镜像携带情况不同）
#define CnIsl CompilerPath + "\Languages\ChineseSimplified.isl"
#if FileExists(CnIsl)
[Languages]
Name: "chinesesimplified"; MessagesFile: "{#CnIsl}"
#endif

[Files]
Source: "..\..\dist\CostGuard\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\{#CostGuardName}"; Filename: "{app}\CostGuard.exe"
Name: "{autodesktop}\{#CostGuardName}"; Filename: "{app}\CostGuard.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："

[Run]
Filename: "{app}\CostGuard.exe"; Description: "立即启动 {#CostGuardName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 只清理程序目录自身的运行残留；Documents\CostGuardProjects 用户数据永不清除
Type: filesandordirs; Name: "{app}"
