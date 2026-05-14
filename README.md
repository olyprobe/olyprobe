# OlyProbe — Project Summary
*Paste this at the start of a new Claude session to resume work instantly.*
*Last updated: May 2026*

---

## What This Is

**OlyProbe** is a free desktop application by **Computography Lab** that connects to Olympus and OM SYSTEM cameras via WiFi, reads all available camera controls and their permitted values, and saves/applies "Setting Cheats" — snapshots of camera configuration that can be reused, shared, and applied to compatible cameras.

The planned ecosystem includes **OlyComp** (scripted/computational remote camera control, not yet started).

The project has two components:
1. **The website** — static marketing/community site at `https://olyprobe.netlify.app`, code on GitHub at `https://github.com/olyprobe/olyprobe`
2. **The local app** — Python/Flask server bundled as a Windows `.exe` via PyInstaller + NSIS installer, serving a browser-based UI at `localhost:5000`

---

## Tech Stack

| Component | Technology |
|---|---|
| Website | Static HTML/CSS/JS, Netlify, GitHub |
| Local server | Python 3.9, Flask, olympuswifi library |
| Camera communication | OPC HTTP/XML API over WiFi (Device Connection hotspot mode) |
| Executable | PyInstaller `--onefile` |
| Installer | NSIS → `olyprobe-setup.exe` |
| Cheat file format | Proprietary binary `.cheat` (magic header `OLPC`, CRC32) |
| Fonts | DM Serif Display + DM Sans |
| Color palette | Warm paper tones (`#f7f6f0`) + forest green accent (`#2d5a3d`) |

---

## Design System

```
--ink: #1a1a18          --paper: #f7f6f0
--ink-2: #4a4a44        --paper-2: #eeede5
--ink-3: #8a8a80        --paper-3: #e4e3d8
--accent: #2d5a3d       --accent-light: #e8f0ea
--accent-mid: #4a8c62   --rule: #d0cfc4
--serif: 'DM Serif Display'
--sans: 'DM Sans'
```

---

## Repository Structure

**GitHub:** `https://github.com/olyprobe/olyprobe`
**Netlify:** `https://olyprobe.netlify.app`
**Local dev folder:** `C:\Users\tnegr\OlyProbe-Dev\`

```
OlyProbe-Dev/
  server.py                  — Flask server, all endpoints
  olyprobe-local.html        — Main local UI (library, probe, detail views)
  olyprobe-compare.html      — Comparison table view at /compare
  olyprobe-installer.nsi     — NSIS installer script
  olyprobe.ico               — App icon (placeholder, green circle with O)
  LICENSE.txt                — MIT-style license
  .gitignore                 — excludes dist/, build/, __pycache__, *.exe, *.ico, test.py
  dist/
    olyprobe.exe             — PyInstaller output (not in git)
  olyprobe-setup.exe         — NSIS installer output (not in git, uploaded to GitHub Releases)

Website files (also in OlyProbe-Dev, deployed via Netlify):
  index.html                 — Homepage, complete
  download.html              — Download page, setup steps 2-4 still lorem ipsum
  cheats.html                — Cheats library page, sample data hardcoded
  community.html             — Community page, stubs
  about.html                 — About page, complete
  README.md                  — Project summary (this document)
```

---

## Camera Connection

**Method:** WiFi Device Connection (camera hotspot mode only for beta)
**Camera IP:** `192.168.0.10` (fixed — camera creates hotspot)
**Library:** `from olympuswifi.camera import OlympusCamera`

**One-time setup per camera:**
1. Put camera in Device Connection WiFi mode
2. Pair with OI.Share app on phone/tablet — only needed once, tablet not required after
3. Connect PC to camera's WiFi hotspot network
4. Launch OlyProbe

**Key API calls:**
```python
cam = OlympusCamera()                                    # connects to 192.168.0.10
cam.send_command('switch_cammode', mode='rec', lvqty='0320x0240')
cam.send_command('get_camprop', com='desc', propname='desclist')  # full property list
cam.send_command('get_camprop', com='desc', propname=name)        # single property
cam.send_command('set_camprop', com='set', propname=name, value=val)
cam.send_command('get_caminfo')                          # model/firmware XML
```

**desclist response format (one call returns everything):**
```xml
<desclist>
  <desc>
    <propname>takemode</propname>
    <attribute>getset</attribute>   <!-- or "get" for read-only -->
    <value>M</value>                <!-- current value -->
    <enum>P A S M movie B</enum>    <!-- space-separated allowed values -->
  </desc>
  ...
</desclist>
```

**XML value extraction:**
```python
import xml.etree.ElementTree as ET
root = ET.fromstring(response.text)
for desc in root.findall('desc'):
    name      = desc.findtext('propname')
    access    = desc.findtext('attribute')   # 'getset' or 'get'
    current   = desc.findtext('value')
    enum_text = desc.findtext('enum') or ''
    allowed   = enum_text.split() if enum_text.strip() else []
```

---

## Flask Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/` | GET | Serves olyprobe-local.html |
| `/compare` | GET | Serves olyprobe-compare.html |
| `/api/connect` | POST | Connect to camera `{method: wifi\|usb}` |
| `/api/disconnect` | POST | Disconnect camera |
| `/api/probe` | POST | Probe camera via desclist, returns controls array |
| `/api/prop_labels` | GET | Returns current PROP_LABELS dict for dynamic label lookup |
| `/api/cheats` | GET | List all local .cheat files |
| `/api/cheats/<id>` | GET | Get full cheat data |
| `/api/cheats/<id>` | DELETE | Delete cheat file |
| `/api/cheats/<id>/set_pending` | POST | Set/clear upload_pending flag |
| `/api/import_cheat` | POST | Drag-drop import, validates magic header + CRC, deduplicates by checksum |
| `/api/save_cheat` | POST | Save probe data as .cheat file |
| `/api/apply_cheat` | POST | Write cheat settings to camera |
| `/api/upload_cheat` | POST | Upload to community (stub — COMMUNITY_UPLOAD_URL not set) |
| `/api/open_cheats_folder` | POST | Opens ~/OlyProbe/Cheats/ in Windows Explorer |
| `/api/sync_status` | GET | Returns online status, pending count, last sync time |
| `/api/sync_now` | POST | Triggers immediate background sync attempt |
| `/api/compare` | GET | Get full cheat data for comparison session |
| `/api/compare/add` | POST | Add cheat to comparison (auto-purges stale entries) |
| `/api/compare/remove` | POST | Remove cheat from comparison |
| `/api/compare/clear` | POST | Clear comparison session |

---

## .cheat File Format

```
4 bytes  magic       b"OLPC"
2 bytes  version     uint16 = 1
4 bytes  meta_len    uint32
N bytes  meta_json   {
                       category, description, camera_model, firmware,
                       captured_at, upload_pending, uploaded
                     }
4 bytes  data_len    uint32
N bytes  data_json   [{ name, label, access, current_value, allowed_values }, ...]
4 bytes  checksum    uint32 CRC32
```

**Local storage:** `~/OlyProbe/Cheats/` (created on first run)
**Filename format:** `{YYYYMMDDTHHMMSS}_{slug}.cheat`
**Duplicate detection:** CRC32 checksum comparison on import

---

## Build Commands

**PyInstaller (run from OlyProbe-Dev):**
```
Stop-Process -Name olyprobe -Force -ErrorAction SilentlyContinue
pyinstaller --onefile --noconsole --icon=olyprobe.ico --add-data "olyprobe-local.html;." --add-data "olyprobe-compare.html;." --name "olyprobe" server.py
```

**NSIS installer:**
```
& "C:\Program Files (x86)\NSIS\makensis.exe" olyprobe-installer.nsi
```

**Output:** `olyprobe-setup.exe` in `OlyProbe-Dev\`

**Kill background instances (do before every rebuild):**
```
Stop-Process -Name olyprobe -Force -ErrorAction SilentlyContinue
```

**Important:** If Stop-Process gives "access denied", run PowerShell as Administrator.

---

## GitHub Release

**URL:** `https://github.com/olyprobe/olyprobe/releases/tag/v0.1-beta`
**Asset:** `olyprobe-setup.exe`
**Download button URL:** `https://github.com/olyprobe/olyprobe/releases/download/v0.1-beta/olyprobe-setup.exe`

---

## Supported Cameras

### Full support (WiFi OPC API via Device Connection)
**Current OM SYSTEM:** OM-1 Mark II, OM-1, OM-5 Mark II, OM-5, OM-3, OM-D E-M10 Mark IV

**Recent discontinued Olympus:** OM-D E-M1X, E-M1 Mark III, E-M1 Mark II, E-M1, E-M5 Mark III, E-M5 Mark II, E-M10 Mark III, E-M10 Mark II, E-M10

### USB tethering (pro bodies only, not yet implemented)
OM-1 Mark II, OM-1, E-M1X, E-M1 Mark III, E-M1 Mark II

### Development / test cameras
- **OM SYSTEM OM-1 Mark II** — primary, owned, fully tested
- **Olympus OM-D E-M10 Mark IV** — secondary, owned, fully tested
- **Olympus E-PL8** — probe-only (restricted WiFi, can't maintain session for set operations)

---

## Known Property Set — Three Camera Comparison

From real probe data (see `olyprobe-comparison.csv`):

**Universal — present on all three cameras (16 properties):**
`touchactiveframe`, `takemode`, `noisereduction`, `lowvibtime`, `bulbtimelimit`, `digitaltelecon`, `cameradrivemode`, `drivemode`, `focalvalue`, `expcomp`, `shutspeedvalue`, `isospeedvalue`, `wbvalue`, `artfilter`, `exposemovie`, `colorphase`

**E-M10 Mark IV only:**
`SilentTime`, `SceneSub`, `NoiseReductionExposureTime`, `SilentNoiseReduction`, all 12 `ArtEffectType*` variants

**OM-1 Mark II only:**
`supermacrozoom`, `QualityMovie2`, `ValidMediaSlot`

**E-PL8 only:**
`colortone` (Picture Mode), `qualitymovie`

**Notable value differences:**
- `takemode`: OM-1 adds `B` (Bulb); E-PL8/E-M10 don't have it
- `isospeedvalue`: OM-1 goes to ISO 102400; E-M10 to 25600; E-PL8 lowest range
- `expcomp`: OM-1 full ±5.0 range; E-M10 only shows 0.0 (bug or limitation TBD)
- `digitaltelecon`: OM-1 has 1.4x and 2.0x options; others just on/off
- `lowvibtime`: OM-1 value `-1` means disabled; others show `0`

---

## UI Features (olyprobe-local.html)

**Library view:**
- Grid of .cheat cards with category color band
- Four buttons per card: **View**, **Load**, **Compare**, **Delete**
- Upload checkbox per card ("Share to community") — persists in .cheat file
- "✓ Uploaded" badge replaces checkbox after successful upload
- Sync bar showing pending count and online status with "Sync now" button
- Header buttons: **Open folder** (opens Cheats folder in Explorer), **Compare** (opens compare tab), **Probe camera**
- Drag-and-drop import anywhere on window — validates magic header + CRC, deduplicates

**View overlay:** Popup showing full controls table for a cheat without loading it to camera

**Detail/Apply view:** Controls table + "Send to camera" button + "Upload to library" button

**Probe/Create view:** Probe camera → controls display → save form (category + description) → Save .cheat

**Controls table (all views):** Property API name + human label, current value highlighted in allowed values chips, read-only badge

---

## UI Features (olyprobe-compare.html)

- Multi-column comparison table, one column per cheat (max 6)
- First column sticky with property label + API name
- Amber highlight = values differ; pink highlight = property missing in that cheat
- Diff summary bar (total props, diff count, missing count)
- Text legend: "Value differs across Cheats (Yellow) / Property not present (Pink)"
- Click any value cell → popover showing all allowed values with current highlighted
- Header buttons: **Export CSV**, **Export PDF** (browser print), **Copy link** (snapshot URL), **Clear all**, **← Library**
- Polls server every 2 seconds for session updates from main UI
- URL snapshot: `/compare?ids=id1,id2,id3`

---

## Auto-Sync Architecture

- Background thread in server checks internet every 60 seconds
- When online, uploads all cheats with `upload_pending: true` to `COMMUNITY_UPLOAD_URL`
- On success: sets `upload_pending: false`, `uploaded: true`, rewrites `.cheat` file
- `COMMUNITY_UPLOAD_URL = None` in server.py — set this when community backend is ready
- `/api/sync_now` triggers immediate sync
- Browser polls `/api/sync_status` every 30 seconds for UI updates

---

## Known Issues / Bugs

1. **Art Filter and SceneSub labels** — E-M10 cheats saved before label update show raw API names (`ArtEffectTypePopart` etc.) instead of friendly labels. Dynamic `PROP_LABELS` lookup via `/api/prop_labels` is implemented but timing issue prevents it applying to old cheats in compare/view. New cheats from fresh probes should be correct.

2. **expcomp on E-M10** — only shows `0.0` as allowed value. May be a camera API limitation or a bug in how the E-M10 reports the property.

3. **USB tethering** — not implemented. OM Capture uses proprietary MTP operation codes (e.g. `0x948A`) — requires protocol reverse engineering. Deferred.

4. **Open folder button** — opens in top-left corner behind browser. Foreground focus not achievable reliably from Flask/PowerShell. Acceptable for now.

5. **Download page setup steps 2-4** — still lorem ipsum. Awaiting real copy.

6. **WiFi session drops on mode change** — changing shooting mode on camera body during active WiFi session drops the connection. Workflow: set camera first, then connect.

---

## Pending Work

### Immediate
- [ ] Write download page setup steps 2-4 (real connection workflow now known)
- [ ] Property grouping design for controls display (now have real data from 3 cameras)
- [ ] Fix Art Filter label timing issue

### Short term
- [ ] Community backend — upload endpoint, Cheat library database
- [ ] Capability database — normalized per-model property/value records
- [ ] Camera capability explorer page (SEO asset)
- [ ] Cross-camera compatibility layer

### Medium term
- [ ] USB tethering (proprietary MTP reverse engineering)
- [ ] Mac and Linux builds
- [ ] Code signing certificate

### Long term
- [ ] OlyComp remote control app
- [ ] Bluetooth connection management

---

## Key Decisions & Reasoning

| Decision | Reasoning |
|---|---|
| WiFi Device Connection hotspot only for beta | PC Connection WiFi is image-transfer only; USB requires proprietary MTP reverse engineering |
| OI.Share one-time pairing | Required by OM-1 firmware; only needed once per camera |
| Python 3.9 (not 3.14) | ptpy compatibility; 3.14 has construct/collections.abc issues |
| PyInstaller + NSIS installer | Proper Windows install with Start Menu, file association, Add/Remove Programs |
| desclist probe approach | Single API call returns all properties, current values, and permitted values |
| ~/OlyProbe/Cheats/ folder | Predictable, user-accessible, .cheat files shareable directly |
| .cheat binary format | Proprietary with magic header + CRC32 for validation and duplicate detection |
| Dynamic PROP_LABELS via /api/prop_labels | Labels always current even for old .cheat files |
| Compare session server-side | Live updates; hybrid with Copy Link for snapshots |
| Separate GitHub org/repo | Clean separation from personal account |
| E-M10 Mark IV as low-end test camera | Current production, full OPC WiFi, widest capability gap from OM-1 |
| E-PL8 excluded from supported list | Restricted WiFi can't maintain session for set operations |
| No PEN/Tough models in supported list | Not target market |

---

## OlyComp (Future Product)

Planned remote control app for scripted/computational camera sequences:
- Bulb-ramped and variable speed time-lapse
- Scripted asymmetrical exposure bracketing
- Multi-setting memory / infinite custom settings
- Live preview with real-time adjustment

**Bluetooth role:** Wake camera and negotiate WiFi automatically (convenience layer only).
**Minimum supported camera:** OM-D E-M10 Mark IV (full OPC WiFi)

---

## Terminology

- **Computography:** photography that uses computation to extend what's possible in-camera
- **High-intent photography:** using clever/complex settings recipes for deliberate creative outcomes
- **Setting Cheat / .cheat file:** a saved camera configuration snapshot
- **Probe:** interrogating a camera's API to read all available controls and values
- **Capability map:** the full set of properties and permitted values for a given camera model
- **Capability database:** community-aggregated capability maps across many camera models (planned)

---

## Contact

- Website: `https://olyprobe.netlify.app`
- Email: `olyprobe@outlook.com`
- GitHub: `https://github.com/olyprobe/olyprobe`
