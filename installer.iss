; Inno Setup script for PC Game Roulette.
;
; Build the exe first (python -m PyInstaller pc-game-roulette.spec --noconfirm),
; then compile this:
;     "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss
; Produces dist\PC Game Roulette Setup.exe.
;
; The installer:
;   * installs per-user by default (no admin/UAC prompt), location selectable
;   * creates Start-menu (and optional desktop) shortcuts
;   * checks for the Microsoft Edge WebView2 Runtime and downloads/installs it
;     from Microsoft only if it's missing
;   * registers a proper uninstaller (offers to remove personal data too)
;
; The app stores its data in %LOCALAPPDATA%\PC Game Roulette, never in the
; install folder — so it works from Program Files and survives updates.

#define MyAppName "PC Game Roulette"
#define MyAppVersion "1.1.1"
#define MyAppPublisher "TheFinalTommy"
#define MyAppExeName "PC Game Roulette.exe"

[Setup]
AppId={{8F3A1C7E-5B92-4D6A-9E3F-1A2B3C4D5E6F}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=PC Game Roulette Setup
SetupIconFile=app.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; Per-user install by default = no UAC prompt; user can switch to all-users.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}";            Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}";  Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}";      Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; Install the WebView2 runtime first, only if it isn't already present.
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; \
  StatusMsg: "Installing Microsoft Edge WebView2 Runtime (required)..."; \
  Flags: waituntilterminated; Check: WebView2Missing
; Offer to launch on finish.
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; \
  Flags: nowait postinstall skipifsilent

[Code]
const
  WV2_CLIENT = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

var
  DownloadPage: TDownloadWizardPage;

function WebView2Installed(): Boolean;
var
  v: String;
begin
  Result :=
    RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\' + WV2_CLIENT, 'pv', v) or
    RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\' + WV2_CLIENT, 'pv', v) or
    RegQueryStringValue(HKCU, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\' + WV2_CLIENT, 'pv', v);
  // A "pv" of 0.0.0.0 means the key exists but the runtime isn't really installed.
  if Result and (v = '0.0.0.0') then
    Result := False;
end;

function WebView2Missing(): Boolean;
begin
  Result := not WebView2Installed();
end;

function OnDownloadProgress(const Url, FileName: String; const Progress, ProgressMax: Int64): Boolean;
begin
  Result := True;
end;

procedure InitializeWizard;
begin
  DownloadPage := CreateDownloadPage(SetupMessage(msgWizardPreparing),
    SetupMessage(msgPreparingDesc), @OnDownloadProgress);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  // Just before installing, grab the WebView2 bootstrapper if needed.
  if (CurPageID = wpReady) and WebView2Missing() then
  begin
    DownloadPage.Clear;
    DownloadPage.Add('https://go.microsoft.com/fwlink/p/?LinkId=2124703',
      'MicrosoftEdgeWebview2Setup.exe', '');
    DownloadPage.Show;
    try
      try
        DownloadPage.Download;
      except
        // Non-fatal: if the download fails (offline, etc.) we continue; the
        // app will still install and prompt the user later if WebView2 is
        // missing.
      end;
    finally
      DownloadPage.Hide;
    end;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    DataDir := ExpandConstant('{localappdata}\{#MyAppName}');
    if DirExists(DataDir) then
    begin
      if MsgBox('Also remove your saved settings, cached data, and Epic login?'
        + #13#10 + '(Choose No to keep them for a future reinstall.)',
        mbConfirmation, MB_YESNO) = IDYES then
        DelTree(DataDir, True, True, True);
    end;
  end;
end;
