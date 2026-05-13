; OlyProbe Windows Installer Script
; Requires NSIS 3.x
; Build: makensis olyprobe-installer.nsi
;
; Prerequisites:
;   - dist\olyprobe.exe must exist (built by PyInstaller)
;   - Place olyprobe-installer.nsi in the same folder as the dist\ directory

;--------------------------------
; General

!define APP_NAME        "OlyProbe"
!define APP_VERSION     "0.1-beta"
!define APP_PUBLISHER   "Computography Lab"
!define APP_URL         "https://olyprobe.netlify.app"
!define APP_EXE         "olyprobe.exe"
!define APP_SETUP_EXE   "olyprobe-setup.exe"
!define CHEAT_EXT       ".cheat"
!define CHEAT_PROGID    "OlyProbe.CheatFile"
!define INSTALL_DIR     "$PROGRAMFILES64\OlyProbe"
!define CHEATS_DIR      "$DOCUMENTS\OlyProbe\Cheats"
!define UNINST_KEY      "Software\Microsoft\Windows\CurrentVersion\Uninstall\OlyProbe"
!define UNINST_ROOT     HKLM

Name            "${APP_NAME} ${APP_VERSION}"
OutFile         "${APP_SETUP_EXE}"
InstallDir      "${INSTALL_DIR}"
InstallDirRegKey HKLM "Software\OlyProbe" "InstallDir"
RequestExecutionLevel admin
BrandingText    "${APP_PUBLISHER}"

;--------------------------------
; Modern UI

!include "MUI2.nsh"
!include "FileFunc.nsh"

!define MUI_ABORTWARNING
!define MUI_ICON          "olyprobe.ico"
!define MUI_UNICON        "olyprobe.ico"
!define MUI_WELCOMEFINISHPAGE_BITMAP_NOSTRETCH
!define MUI_FINISHPAGE_RUN         "$INSTDIR\${APP_EXE}"
!define MUI_FINISHPAGE_RUN_TEXT    "Launch OlyProbe now"
!define MUI_FINISHPAGE_LINK        "Visit olyprobe.netlify.app"
!define MUI_FINISHPAGE_LINK_LOCATION "${APP_URL}"

; Pages
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "LICENSE.txt"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

;--------------------------------
; Installer

Section "OlyProbe" SecMain

  ; Kill any running OlyProbe instance before installing
  nsExec::Exec 'taskkill /F /IM olyprobe.exe'
  Sleep 1000

  SectionIn RO   ; Required — cannot be deselected

  SetOutPath "$INSTDIR"

  ; Copy main executable
  File "dist\${APP_EXE}"

  ; Create Cheats directory in user's Documents
  CreateDirectory "${CHEATS_DIR}"

  ; Write install location to registry
  WriteRegStr HKLM "Software\OlyProbe" "InstallDir" "$INSTDIR"
  WriteRegStr HKLM "Software\OlyProbe" "Version"    "${APP_VERSION}"

  ; Register .cheat file association
  WriteRegStr HKCR "${CHEAT_EXT}"               ""                  "${CHEAT_PROGID}"
  WriteRegStr HKCR "${CHEAT_PROGID}"            ""                  "OlyProbe Setting Cheat"
  WriteRegStr HKCR "${CHEAT_PROGID}\DefaultIcon" ""                 "$INSTDIR\${APP_EXE},0"
  WriteRegStr HKCR "${CHEAT_PROGID}\shell\open\command" ""          '"$INSTDIR\${APP_EXE}" "%1"'

  ; Notify Windows of file association change
  System::Call 'shell32.dll::SHChangeNotify(i, i, i, i) v (0x08000000, 0, 0, 0)'

  ; Add/Remove Programs entry
  WriteRegStr   ${UNINST_ROOT} "${UNINST_KEY}" "DisplayName"     "${APP_NAME}"
  WriteRegStr   ${UNINST_ROOT} "${UNINST_KEY}" "DisplayVersion"  "${APP_VERSION}"
  WriteRegStr   ${UNINST_ROOT} "${UNINST_KEY}" "Publisher"       "${APP_PUBLISHER}"
  WriteRegStr   ${UNINST_ROOT} "${UNINST_KEY}" "URLInfoAbout"    "${APP_URL}"
  WriteRegStr   ${UNINST_ROOT} "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr   ${UNINST_ROOT} "${UNINST_KEY}" "UninstallString" "$INSTDIR\uninstall.exe"
  WriteRegStr   ${UNINST_ROOT} "${UNINST_KEY}" "DisplayIcon"     "$INSTDIR\${APP_EXE}"
  WriteRegDWORD ${UNINST_ROOT} "${UNINST_KEY}" "NoModify"        1
  WriteRegDWORD ${UNINST_ROOT} "${UNINST_KEY}" "NoRepair"        1

  ; Get installed size for Add/Remove Programs
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0
  WriteRegDWORD ${UNINST_ROOT} "${UNINST_KEY}" "EstimatedSize" "$0"

  ; Create uninstaller
  WriteUninstaller "$INSTDIR\uninstall.exe"

  ; Start Menu shortcut
  CreateDirectory "$SMPROGRAMS\OlyProbe"
  CreateShortcut  "$SMPROGRAMS\OlyProbe\OlyProbe.lnk" "$INSTDIR\${APP_EXE}" "" "$INSTDIR\${APP_EXE}" 0
  CreateShortcut  "$SMPROGRAMS\OlyProbe\Uninstall OlyProbe.lnk" "$INSTDIR\uninstall.exe"

SectionEnd

;--------------------------------
; Optional Desktop Shortcut

Section /o "Desktop shortcut" SecDesktop
  CreateShortcut "$DESKTOP\OlyProbe.lnk" "$INSTDIR\${APP_EXE}" "" "$INSTDIR\${APP_EXE}" 0
SectionEnd

;--------------------------------
; Uninstaller

Section "Uninstall"

  ; Remove main executable and uninstaller
  Delete "$INSTDIR\${APP_EXE}"
  Delete "$INSTDIR\uninstall.exe"
  RMDir  "$INSTDIR"

  ; Remove Start Menu shortcuts
  Delete "$SMPROGRAMS\OlyProbe\OlyProbe.lnk"
  Delete "$SMPROGRAMS\OlyProbe\Uninstall OlyProbe.lnk"
  RMDir  "$SMPROGRAMS\OlyProbe"

  ; Remove Desktop shortcut if it exists
  Delete "$DESKTOP\OlyProbe.lnk"

  ; Remove file association
  DeleteRegKey HKCR "${CHEAT_EXT}"
  DeleteRegKey HKCR "${CHEAT_PROGID}"

  ; Remove registry entries
  DeleteRegKey HKLM "Software\OlyProbe"
  DeleteRegKey ${UNINST_ROOT} "${UNINST_KEY}"

  ; Notify Windows of file association removal
  System::Call 'shell32.dll::SHChangeNotify(i, i, i, i) v (0x08000000, 0, 0, 0)'

  ; Note: We do NOT delete ~/OlyProbe/Cheats/ — user's cheat files are preserved

  MessageBox MB_ICONINFORMATION "OlyProbe has been uninstalled.$\n$\nYour Setting Cheats in Documents\OlyProbe\Cheats have been preserved."

SectionEnd
