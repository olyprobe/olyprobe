# OlyProbe — Project Summary / README
*Paste this at the start of a new Claude session to resume work instantly.*
*Last updated: May 2026*

---

## What This Is

**OlyProbe** is a free desktop application by **Computography Lab** that connects to Olympus and OM SYSTEM cameras via WiFi, reads all available camera controls and their permitted values, and saves/applies "Setting Cheats" — snapshots of camera configuration that can be reused, shared, and applied to compatible cameras.

The planned ecosystem includes **OlyComp** (scripted/computational remote camera control, not yet started).

**Website:** `https://olyprobe.netlify.app`
**GitHub:** `https://github.com/olyprobe/olyprobe`
**Contact:** `olyprobe@outlook.com`

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

## Repository & File Structure

**Local dev folder:** `C:\Users\tnegr\OlyProbe-Dev\`

```
OlyProbe-Dev/
  server.py                  — Flask server, all endpoints
  olyprobe-local.html        — Main local UI
  olyprobe-compare.html      — Comparison table view at /compare
  olyprobe-installer.nsi     — NSIS installer script
  olyprobe.ico               — App icon (placeholder)
  LICENSE.txt
  README.md                  — This file
  .gitignore                 — excludes dist/, build/, __pycache__, *.exe, *.ico, test.py
  dist/olyprobe.exe          — PyInstaller output (not in git)
  olyprobe-setup.exe         — NSIS installer (not in git, uploaded to GitHub Releases)

Website files (deployed via Netlify):
  index.html                 — Homepage, complete
  download.html              — Download page, setup steps 2-4 still lorem ipsum
  cheats.html                — Cheats library page, sample data hardcoded
  community.html             — Community page, stubs
  about.html                 — About page, complete
```

---

## Camera Connection

**Method:** WiFi Device Connection (camera hotspot mode only for beta)
**Camera IP:** `192.168.0.10` (fixed)
**Library:** `from olympuswifi.camera import OlympusCamera`

**One-time setup per camera:**
1. Put camera in Device Connection WiFi mode
2. Pair with OI.Share app on phone/tablet (once only)
3. Connect PC to camera's WiFi hotspot
4. Launch OlyProbe

**Key API calls:**
```python
cam = OlympusCamera()
cam.send_command('switch_cammode', mode='rec', lvqty='0320x0240')
cam.send_command('get_camprop', com='desc', propname='desclist')  # full property list
cam.send_command('set_camprop', com='set', propname=name, value=val)
cam.send_command('get_caminfo')
```

**desclist response — one call returns everything:**
```xml
<desclist>
  <desc>
    <propname>takemode</propname>
    <attribute>getset</attribute>   <!-- or "get" for read-only -->
    <value>M</value>
    <enum>P A S M movie B</enum>    <!-- space-separated allowed values -->
  </desc>
</desclist>
```

---

## Flask Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/` | GET | Serves olyprobe-local.html |
| `/compare` | GET | Serves olyprobe-compare.html |
| `/api/connect` | POST | Connect to camera |
| `/api/disconnect` | POST | Disconnect camera |
| `/api/probe` | POST | Probe via desclist, returns controls array |
| `/api/prop_labels` | GET | Current PROP_LABELS for dynamic label lookup |
| `/api/ontology` | GET | Property grouping definition |
| `/api/cheats` | GET | List all local .cheat files |
| `/api/cheats/<id>` | GET | Get full cheat data |
| `/api/cheats/<id>` | DELETE | Delete cheat file |
| `/api/cheats/<id>/set_pending` | POST | Set/clear upload_pending flag |
| `/api/cheats/<id>/update_meta` | POST | Update category, description, notes |
| `/api/cheats/order` | POST | Save custom card order |
| `/api/cheats/order` | DELETE | Reset to default oldest-first order |
| `/api/import_cheat` | POST | Drag-drop import, validates + deduplicates |
| `/api/save_cheat` | POST | Save probe data as .cheat (auto-suffixes duplicates) |
| `/api/load_cheat` | POST | Live probe + cheat comparison for Load modal |
| `/api/apply_cheat` | POST | Apply cheat directly to camera |
| `/api/apply_settings` | POST | Apply list of {name,value} pairs to camera |
| `/api/open_cheats_folder` | POST | Opens ~/OlyProbe/Cheats/ in Explorer |
| `/api/sync_status` | GET | Online status, pending count, last sync |
| `/api/sync_now` | POST | Trigger immediate sync |
| `/api/compare` | GET | Get comparison session cheats |
| `/api/compare/add` | POST | Add cheat to comparison |
| `/api/compare/remove` | POST | Remove cheat from comparison |
| `/api/compare/clear` | POST | Clear comparison session |

---

## .cheat File Format

```
4 bytes  magic       b"OLPC"
2 bytes  version     uint16 = 1
4 bytes  meta_len    uint32
N bytes  meta_json   {
                       category, description, notes,
                       camera_model, firmware, captured_at,
                       upload_pending, uploaded
                     }
4 bytes  data_len    uint32
N bytes  data_json   [{ name, label, access, current_value, allowed_values }, ...]
4 bytes  checksum    uint32 CRC32
```

**Local storage:** `~/OlyProbe/Cheats/`
**Filename:** `{YYYYMMDDTHHMMSS}_{slug}.cheat`
**Order file:** `~/OlyProbe/Cheats/.order.json` (custom drag order, oldest-first default)
**Duplicate detection:** CRC32 checksum on import; description auto-suffix (2),(3) on save

---

## Build Commands

```
# Kill background instances first
Stop-Process -Name olyprobe -Force -ErrorAction SilentlyContinue

# PyInstaller
pyinstaller --onefile --noconsole --icon=olyprobe.ico --add-data "olyprobe-local.html;." --add-data "olyprobe-compare.html;." --name "olyprobe" server.py

# NSIS installer
& "C:\Program Files (x86)\NSIS\makensis.exe" olyprobe-installer.nsi
```

Note: `server.py` auto-kills running `olyprobe.exe` on startup so `python server.py` always takes over cleanly.

---

## GitHub Release

**URL:** `https://github.com/olyprobe/olyprobe/releases/tag/v0.1-beta`
**Asset:** `olyprobe-setup.exe`
**Download URL:** `https://github.com/olyprobe/olyprobe/releases/download/v0.1-beta/olyprobe-setup.exe`

---

## Supported Cameras

### Full support (WiFi OPC API)
**Current OM SYSTEM:** OM-1 Mark II, OM-1, OM-5 Mark II, OM-5, OM-3, OM-D E-M10 Mark IV

**Recent discontinued Olympus:** OM-D E-M1X, E-M1 Mark III, E-M1 Mark II, E-M1, E-M5 Mark III, E-M5 Mark II, E-M10 Mark III, E-M10 Mark II, E-M10

### USB tethering (pro bodies, not yet implemented)
OM-1 Mark II, OM-1, E-M1X, E-M1 Mark III, E-M1 Mark II

### Development cameras
- **OM SYSTEM OM-1 Mark II** — primary, owned, fully tested
- **Olympus OM-D E-M10 Mark IV** — secondary, owned, fully tested
- **Olympus E-PL8** — probe-only (restricted WiFi)

**Note:** PEN models (E-P7, PEN-F, E-P5, E-PL10, E-PL9) to be added back to supported cameras popup — excluded incorrectly based on E-PL8 experience.

---

## Known Property Set — Three Camera Comparison

**Settable properties (getset) on OM-1 Mark II:**
`takemode`, `shutspeedvalue`, `focalvalue`, `isospeedvalue`, `expcomp`, `drivemode`, `wbvalue`, `exposemovie`, `artfilter`, `colorphase`
*(approximately 9-10 settable — exact count under investigation, see Known Issues)*

**Read-only properties (get) on OM-1:**
`touchactiveframe`, `lowvibtime`, `digitaltelecon`, `supermacrozoom`, `cameradrivemode`, `SilentTime`, `QualityMovie2`, `NoiseReductionExposureTime`, `SilentNoiseReduction`, `ValidMediaSlot`, `noisereduction`, `bulbtimelimit`

**E-M10 Mark IV additional settable:**
`SceneSub`, all 12 `ArtEffectType*` variants

**E-PL8 only:**
`colortone`, `qualitymovie`

**Notable value differences:**
- `takemode`: OM-1 adds `B` (Bulb)
- `isospeedvalue`: OM-1 goes to ISO 102400; E-M10 to 25600
- `expcomp`: OM-1 full ±5.0; E-M10 only shows 0.0 (possible bug)
- `digitaltelecon`: OM-1 has 1.4x/2.0x; others on/off only

---

## UI Features (olyprobe-local.html)

### Library view
- Grid of .cheat cards with category color band
- **Drag to reorder** — grab cursor on card, drop to reorder; persists via `.order.json`
- **Reset order** button appears when custom order is active (oldest-first default)
- **Notes tooltip** — hover card body to see notes (500ms delay), with "Notes" header
- Red circle **compare indicator** on cards in comparison session — click to remove
- Four card buttons: **View**, **Load**, **Compare**, **Delete**
- **Share to community** checkbox per card (persists in .cheat file)
- **✓ Uploaded** badge replaces checkbox after successful upload
- Sync bar: pending count, online status, Sync now button
- Header buttons: **Reset order** (when active), **Probe camera**, **Compare**, **Share cheats**
- Drag-and-drop `.cheat` file import — validates, deduplicates by CRC32

### View overlay
- Opens from View button — shows full cheat without camera connection
- **Editable header:** category dropdown + description field
- **Notes textarea** — editable, saved on Save changes
- Grouped collapsible controls table (Exposure/Drive expanded by default)
- **Save changes** — overwrites existing cheat
- **Save as new Cheat** — uses current header fields, auto-suffixes if description unchanged

### Load modal
- Opens from Load button — requires camera connected
- Auto-probes connected camera on open
- Shows cheat metadata + source→target camera models
- Three compatibility sections:
  - **Ready** — value available, pre-selected dropdown (user can override)
  - **No exact match** — value unavailable, dropdown with "Leave unchanged" option
  - **Not applicable** — property not on target camera, informational only
- Summary: "X settings will be applied · Y pending your choice · Z skipped"
- **Apply to camera** button

### Probe/Create view
- Probe camera → grouped collapsible controls display
- Save form: category, description, notes
- Form fields clear after save

### Compare tab (`/compare`)
- Multi-column table, one column per cheat (max 6)
- Amber = values differ; Pink = property missing
- Click value cell → popover showing all allowed values
- Export CSV, Export PDF (browser print), Copy link
- Remove column via × button on column header

---

## Ontology (Property Groups)

Defined in `ONTOLOGY` list in `server.py`. Served via `/api/ontology`.
Properties not in any group appear in **Other** automatically.

| Group | Default | Properties |
|---|---|---|
| Exposure | Expanded | takemode, shutspeedvalue, focalvalue, isospeedvalue, expcomp, exposemovie, bulbtimelimit, wbvalue |
| Drive & Timing | Expanded | drivemode, lowvibtime, SilentTime |
| Focus | Collapsed | afmode, afarea, facedetect, eyedetect, touchactiveframe, digitaltelecon, supermacrozoom, focal35mm |
| Creative | Collapsed | artfilter, colortone, colorphase, SceneSub, all ArtEffectType* |
| Image Quality | Collapsed | imagequality, imagesize, colorspace, noisereduction, NoiseReductionExposureTime, SilentNoiseReduction, noisefilter |
| Video | Collapsed | QualityMovie2, qualitymovie |
| Camera Status | Collapsed | cameradrivemode, remainshots, batterylevel, ValidMediaSlot, modeinfo, mediaid, recview, liveviewquality, destination |
| Advanced/Computational | Collapsed | bracketmode, focusbracket, hdrshooting, multiexposure, pixelshift, intervaltime, bulbtime, stardetect, digitalzoom, antiflicker |

---

## Auto-Sync Architecture

- Background thread checks internet every 60 seconds
- Uploads cheats with `upload_pending: true` to `COMMUNITY_UPLOAD_URL`
- `COMMUNITY_UPLOAD_URL = None` in server.py — set when community backend is ready
- On success: sets `upload_pending: false`, `uploaded: true`
- Browser polls `/api/sync_status` every 30 seconds

---

## Known Issues / Bugs

1. **Load modal shows fewer properties than expected** — when loading an OM-1 cheat into an OM-1, Load shows ~8 properties instead of all settable ones. Under investigation — may be related to which properties have `getset` vs `get` access in the cheat file.

2. **Art Filter labels on old E-M10 cheats** — `ArtEffectType*` properties show raw API names instead of friendly labels in View/Compare for cheats saved before dynamic label lookup was implemented. New cheats are correct.

3. **expcomp on E-M10** — only shows `0.0` as allowed value. May be camera API limitation.

4. **Download page setup steps 2-4** — still lorem ipsum. Awaiting real copy.

5. **USB tethering** — not implemented. OM Capture uses proprietary MTP op codes. Deferred.

6. **Open folder button** — opens in top-left corner behind browser. Acceptable for now.

---

## Pending Work

### Immediate
- [ ] Investigate Load modal showing fewer properties than expected
- [ ] Write download page setup steps 2-4
- [ ] Add PEN models back to supported cameras popup

### Short term
- [ ] Community backend — upload endpoint, Cheat library database
- [ ] Capability database — normalized per-model property/value records
- [ ] Camera capability explorer page (SEO asset)
- [ ] Cross-camera compatibility layer improvements

### Medium term
- [ ] USB tethering
- [ ] Mac and Linux builds
- [ ] Code signing certificate
- [ ] Lens data capture (requires USB tethering)

### Long term
- [ ] OlyComp remote control app
- [ ] Bluetooth connection management

---

## Key Decisions & Reasoning

| Decision | Reasoning |
|---|---|
| WiFi Device Connection only for beta | PC Connection WiFi is image-transfer only; USB needs MTP reverse engineering |
| OI.Share one-time pairing | Required by OM-1 firmware; only needed once per camera |
| Python 3.9 | ptpy compatibility; 3.14 has construct/collections issues |
| PyInstaller + NSIS | Proper install with Start Menu, file association, Add/Remove Programs |
| desclist probe | Single API call returns all properties, current values, permitted values |
| ~/OlyProbe/Cheats/ | Predictable, user-accessible, shareable |
| .cheat binary format | Magic header + CRC32 for validation and duplicate detection |
| Dynamic PROP_LABELS via /api/prop_labels | Labels always current even for old .cheat files |
| Ontology server-side | Single definition used by all UIs; Other group catches new properties |
| Load modal always shows | Even same-camera loads go through compatibility modal for consistent UX |
| Drag reorder oldest-first default | Natural append order; Reset order button when customized |
| Notes field | Context for settings — shooting conditions, intent, tips |
| Compare session persistent | Deliberate per-item add/remove via red checkmark |
| No Clear all in compare | Intentional — individual removal is more deliberate |
| E-PL8 probe-only | Restricted WiFi, can't maintain session |

---

## OlyComp (Future Product)

Planned remote control app for scripted/computational camera sequences:
- Bulb-ramped and variable speed time-lapse
- Scripted asymmetrical exposure bracketing
- Multi-setting memory / infinite custom settings
- Live preview with real-time adjustment

**Minimum supported camera:** OM-D E-M10 Mark IV
**Bluetooth role:** Wake camera and negotiate WiFi (convenience layer only)

---

## Terminology

- **Computography:** photography that uses computation to extend what's possible
- **High-intent photography:** using complex settings recipes for deliberate creative outcomes
- **Setting Cheat / .cheat file:** a saved camera configuration snapshot
- **Probe:** interrogating a camera's API to read all available controls and values
- **Capability map:** full property set and permitted values for a camera model
- **Capability database:** community-aggregated capability maps (planned)
