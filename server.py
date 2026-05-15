"""
OlyProbe local server
Serves the offline UI and proxies OPC WiFi API commands to the camera.

Connection: WiFi Device Connection (hotspot) mode only for beta.
Camera creates hotspot at 192.168.0.10. Connect your PC to the camera's
WiFi network before launching OlyProbe.

Run with: python server.py
Then open: http://localhost:5000
"""

import os
import json
import struct
import zlib
import threading
import webbrowser
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, request, jsonify, send_file, Response
from olympuswifi.camera import OlympusCamera

# ── PATHS ─────────────────────────────────────────────────────────────────────

BASE_DIR     = Path(__file__).parent.resolve()
UI_FILE      = BASE_DIR / "olyprobe-local.html"
COMPARE_FILE = BASE_DIR / "olyprobe-compare.html"
CHEATS_DIR   = Path.home() / "OlyProbe" / "Cheats"
CHEATS_DIR.mkdir(parents=True, exist_ok=True)

# Community upload endpoint (stub — replace with real URL when backend is ready)
COMMUNITY_UPLOAD_URL = None   # e.g. "https://api.olyprobe.com/cheats/upload"

# ── CHEAT FILE FORMAT ─────────────────────────────────────────────────────────
#
# Binary format with magic header for file validation.
# Layout:
#   4 bytes  magic       b"OLPC"
#   2 bytes  version     uint16  currently 1
#   4 bytes  meta_len    uint32  length of JSON metadata block
#   N bytes  meta_json   UTF-8 JSON  { category, description, camera_model,
#                                      firmware, captured_at,
#                                      upload_pending, uploaded }
#   4 bytes  data_len    uint32  length of JSON controls block
#   N bytes  data_json   UTF-8 JSON  [ { name, label, access,
#                                        current_value, allowed_values }, ... ]
#   4 bytes  checksum    uint32  CRC32 of everything above

MAGIC   = b"OLPC"
VERSION = 1

def write_cheat(path: Path, meta: dict, controls: list):
    meta_bytes = json.dumps(meta,     ensure_ascii=False).encode("utf-8")
    data_bytes = json.dumps(controls, ensure_ascii=False).encode("utf-8")
    header  = MAGIC + struct.pack(">H", VERSION)
    payload = (
        struct.pack(">I", len(meta_bytes)) + meta_bytes +
        struct.pack(">I", len(data_bytes)) + data_bytes
    )
    raw = header + payload
    crc = zlib.crc32(raw) & 0xFFFFFFFF
    path.write_bytes(raw + struct.pack(">I", crc))

def read_cheat(path: Path):
    raw = path.read_bytes()
    if len(raw) < 10 or raw[:4] != MAGIC:
        raise ValueError("Not a valid .cheat file")
    version = struct.unpack(">H", raw[4:6])[0]
    if version != VERSION:
        raise ValueError(f"Unsupported .cheat version: {version}")
    stored_crc   = struct.unpack(">I", raw[-4:])[0]
    computed_crc = zlib.crc32(raw[:-4]) & 0xFFFFFFFF
    if stored_crc != computed_crc:
        raise ValueError("Checksum mismatch — file may be corrupted")
    pos      = 6
    meta_len = struct.unpack(">I", raw[pos:pos+4])[0]; pos += 4
    meta     = json.loads(raw[pos:pos+meta_len].decode("utf-8")); pos += meta_len
    data_len = struct.unpack(">I", raw[pos:pos+4])[0]; pos += 4
    controls = json.loads(raw[pos:pos+data_len].decode("utf-8"))
    return meta, controls

def update_cheat_meta(path: Path, updates: dict):
    """Update metadata fields in an existing .cheat file."""
    meta, controls = read_cheat(path)
    meta.update(updates)
    write_cheat(path, meta, controls)

# ── CAMERA STATE ──────────────────────────────────────────────────────────────

camera_lock   = threading.Lock()
camera_client = None
camera_info   = {}

# Human-readable labels for known OPC property names
PROP_LABELS = {
    # Core exposure controls
    "takemode":           "Shooting Mode",
    "shutspeedvalue":     "Shutter Speed",
    "isospeedvalue":      "ISO Speed",
    "focalvalue":         "Aperture (f-stop)",
    "expcomp":            "Exposure Compensation",
    "drivemode":          "Drive Mode",
    "wbvalue":            "White Balance",
    "exposemovie":        "Movie Exposure Mode",
    # Drive and timing
    "cameradrivemode":    "Current Drive Mode",
    "lowvibtime":         "Anti-Shock Delay (Low Vibration)",
    "SilentTime":         "Anti-Shock Delay (Silent)",
    "bulbtimelimit":      "Bulb Time Limit",
    # Image quality
    "imagequality":       "Image Quality",
    "imagesize":          "Image Size",
    "colorspace":         "Color Space",
    "noisereduction":     "Noise Reduction",
    "NoiseReductionExposureTime": "Noise Reduction Exposure Time",
    "SilentNoiseReduction": "Silent Noise Reduction",
    "QualityMovie2":      "Movie Quality",
    "qualitymovie":       "Movie Quality (Legacy)",
    # Creative modes
    "colortone":          "Picture Mode",
    "artfilter":          "Art Filter",
    "colorphase":         "Color Phase",
    "SceneSub":           "Scene Sub-Mode",
    # Art Filter types
    "ArtEffectTypePopart":          "Pop Art Type",
    "ArtEffectTypeRoughMonochrome": "Rough Monochrome Type",
    "ArtEffectTypeToyPhoto":        "Toy Photo Type",
    "ArtEffectTypeDaydream":        "Daydream Type",
    "ArtEffectTypeCrossProcess":    "Cross Process Type",
    "ArtEffectTypeDramaticTone":    "Dramatic Tone Type",
    "ArtEffectTypeLigneClair":      "Ligne Clair Type",
    "ArtEffectTypePastel":          "Pastel Type",
    "ArtEffectTypeMiniature":       "Miniature Type",
    "ArtEffectTypeVintage":         "Vintage Type",
    "ArtEffectTypePartcolor":       "Part Color Type",
    "ArtEffectTypeBleachBypass":    "Bleach Bypass Type",
    "ArtEffectTypeFantasicFocus":   "Fantastic Focus Type",
    "ArtEffectTypeLightTone":       "Light Tone Type",
    "ArtEffectTypeGentleSepia":     "Gentle Sepia Type",
    # Focus and AF
    "afmode":             "AF Mode",
    "afarea":             "AF Area",
    "facedetect":         "Face Detection",
    "eyedetect":          "Eye Detection",
    "touchactiveframe":   "Touch AF Frame Position",
    "digitaltelecon":     "Digital Teleconverter",
    "supermacrozoom":     "Super Macro Zoom",
    "focal35mm":          "Focal Length (35mm equiv)",
    # Bracketing and computational
    "bracketmode":        "Bracket Mode",
    "bracketnum":         "Bracket Count",
    "bracketstep":        "Bracket Step",
    "livecomposite":      "Live Composite",
    "focusbracket":       "Focus Bracket",
    "hdrshooting":        "HDR Shooting",
    "multiexposure":      "Multi Exposure",
    "pixelshift":         "Pixel Shift",
    "stardetect":         "Star Detection",
    "intervaltime":       "Interval Time",
    "intervalnum":        "Interval Count",
    "bulbtime":           "Bulb Timer",
    # Camera status
    "remainshots":        "Remaining Shots",
    "batterylevel":       "Battery Level",
    "mediaid":            "Media ID",
    "modeinfo":           "Mode Info",
    "ValidMediaSlot":     "Active Media Slot",
    "recview":            "Rec View",
    # Misc
    "noisefilter":        "Noise Filter",
    "digitalzoom":        "Digital Zoom",
    "antiflicker":        "Anti-Flicker",
    "liveviewquality":    "Live View Quality",
    "destination":        "Save Destination",
}

# ── ONTOLOGY ──────────────────────────────────────────────────────────────────
# Defines property grouping for display.
# Properties not in any group appear in "Other" automatically.
# Add new properties here as new camera models reveal them.

ONTOLOGY = [
    {
        "id": "exposure",
        "label": "Exposure",
        "default_expanded": True,
        "properties": [
            "takemode", "shutspeedvalue", "focalvalue", "isospeedvalue",
            "expcomp", "exposemovie", "bulbtimelimit", "wbvalue",
        ]
    },
    {
        "id": "drive",
        "label": "Drive & Timing",
        "default_expanded": True,
        "properties": [
            "drivemode", "lowvibtime", "SilentTime",
        ]
    },
    {
        "id": "focus",
        "label": "Focus",
        "default_expanded": False,
        "properties": [
            "afmode", "afarea", "facedetect", "eyedetect",
            "touchactiveframe", "digitaltelecon", "supermacrozoom", "focal35mm",
        ]
    },
    {
        "id": "creative",
        "label": "Creative",
        "default_expanded": False,
        "properties": [
            "artfilter", "colortone", "colorphase", "SceneSub",
            "ArtEffectTypePopart", "ArtEffectTypeRoughMonochrome",
            "ArtEffectTypeToyPhoto", "ArtEffectTypeDaydream",
            "ArtEffectTypeCrossProcess", "ArtEffectTypeDramaticTone",
            "ArtEffectTypeLigneClair", "ArtEffectTypePastel",
            "ArtEffectTypeMiniature", "ArtEffectTypeVintage",
            "ArtEffectTypePartcolor", "ArtEffectTypeBleachBypass",
            "ArtEffectTypeFantasicFocus", "ArtEffectTypeLightTone",
            "ArtEffectTypeGentleSepia",
        ]
    },
    {
        "id": "image_quality",
        "label": "Image Quality",
        "default_expanded": False,
        "properties": [
            "imagequality", "imagesize", "colorspace",
            "noisereduction", "NoiseReductionExposureTime",
            "SilentNoiseReduction", "noisefilter",
        ]
    },
    {
        "id": "video",
        "label": "Video",
        "default_expanded": False,
        "properties": [
            "QualityMovie2", "qualitymovie",
        ]
    },
    {
        "id": "status",
        "label": "Camera Status",
        "default_expanded": False,
        "properties": [
            "cameradrivemode", "remainshots", "batterylevel",
            "ValidMediaSlot", "modeinfo", "mediaid", "recview",
            "liveviewquality", "destination",
        ]
    },
    {
        "id": "computational",
        "label": "Advanced / Computational",
        "default_expanded": False,
        "properties": [
            "bracketmode", "bracketnum", "bracketstep",
            "livecomposite", "hdrshooting", "multiexposure",
            "pixelshift", "focusbracket", "intervaltime",
            "intervalnum", "bulbtime", "stardetect",
            "digitalzoom", "antiflicker",
        ]
    },
]

# Lookup set of all classified property names
ONTOLOGY_PROP_SET = set(p for group in ONTOLOGY for p in group["properties"])

def xml_value(response_text):
    """Extract value from OPC XML response like <get><value>M</value></get>"""
    try:
        root = ET.fromstring(response_text)
        return root.findtext('value')
    except Exception:
        return None

# ── AUTO-SYNC ─────────────────────────────────────────────────────────────────

sync_lock    = threading.Lock()
sync_status  = {"last_sync": None, "pending": 0, "synced": 0}

def check_internet():
    """Quick check for internet connectivity."""
    import socket
    try:
        socket.setdefaulttimeout(3)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("8.8.8.8", 53))
        return True
    except Exception:
        return False

def do_sync():
    """Upload all pending cheats to the community. Runs in background thread."""
    if not COMMUNITY_UPLOAD_URL:
        return   # Community backend not yet configured
    if not check_internet():
        return
    with sync_lock:
        uploaded = 0
        for f in CHEATS_DIR.glob("*.cheat"):
            try:
                meta, controls = read_cheat(f)
                if not meta.get("upload_pending", False):
                    continue
                # POST to community API
                import requests as req
                resp = req.post(
                    COMMUNITY_UPLOAD_URL,
                    files={"cheat": (f.name, f.read_bytes(), "application/octet-stream")},
                    timeout=15
                )
                if resp.ok:
                    update_cheat_meta(f, {"upload_pending": False, "uploaded": True})
                    uploaded += 1
            except Exception:
                pass
        sync_status["last_sync"]  = datetime.now(timezone.utc).isoformat()
        sync_status["synced"]    += uploaded
        sync_status["pending"]    = sum(
            1 for f in CHEATS_DIR.glob("*.cheat")
            if read_cheat_meta_safe(f).get("upload_pending", False)
        )

def read_cheat_meta_safe(path: Path):
    try:
        meta, _ = read_cheat(path)
        return meta
    except Exception:
        return {}

def sync_loop():
    """Background thread: check and sync every 60 seconds."""
    import time
    while True:
        time.sleep(60)
        threading.Thread(target=do_sync, daemon=True).start()

threading.Thread(target=sync_loop, daemon=True).start()

# ── FLASK APP ─────────────────────────────────────────────────────────────────

app = Flask(__name__)

@app.route("/")
def index():
    if UI_FILE.exists():
        return send_file(UI_FILE, max_age=0)
    return "<h2>olyprobe-local.html not found next to server.py</h2>", 404

@app.route("/compare")
def compare_page():
    if COMPARE_FILE.exists():
        return send_file(COMPARE_FILE, max_age=0)
    return "<h2>olyprobe-compare.html not found next to server.py</h2>", 404

# ── CONNECTION ────────────────────────────────────────────────────────────────

@app.route("/api/connect", methods=["POST"])
def api_connect():
    global camera_client, camera_info
    data   = request.get_json(force=True)
    method = data.get("method", "wifi")

    with camera_lock:
        if camera_client:
            try:
                camera_client.send_command('exec_pwoff')
            except Exception:
                pass
            camera_client = None
            camera_info   = {}

        if method == "usb":
            return jsonify(ok=False,
                error="USB tethering is coming in a future release. Please use WiFi."), 200

        try:
            cam = OlympusCamera()
            cam.send_command('switch_cammode', mode='rec', lvqty='0320x0240')
            info_resp = cam.send_command('get_caminfo')
            model    = "OM SYSTEM Camera"
            firmware = "unknown"
            try:
                root     = ET.fromstring(info_resp.text)
                model    = root.findtext('model')    or model
                firmware = root.findtext('firmware') or firmware
            except Exception:
                pass
            camera_client = cam
            camera_info   = {"model": model, "firmware": firmware}
            return jsonify(ok=True, model=model, firmware=firmware)
        except Exception as e:
            return jsonify(ok=False, error=str(e)), 200

@app.route("/api/disconnect", methods=["POST"])
def api_disconnect():
    global camera_client, camera_info
    with camera_lock:
        if camera_client:
            try:
                camera_client.send_command('exec_pwoff')
            except Exception:
                pass
        camera_client = None
        camera_info   = {}
    return jsonify(ok=True)

# ── PROBE ─────────────────────────────────────────────────────────────────────

@app.route("/api/probe", methods=["POST"])
def api_probe():
    with camera_lock:
        if not camera_client:
            return jsonify(ok=False, error="No camera connected"), 200
        try:
            camera_client.send_command('switch_cammode', mode='rec', lvqty='0320x0240')

            # Get full property list via desclist — single call returns everything
            # including current values, access types, and all permitted values
            resp = camera_client.send_command('get_camprop', com='desc', propname='desclist')
            root = ET.fromstring(resp.text)
            controls = []

            for desc in root.findall('desc'):
                name      = desc.findtext('propname') or ''
                access    = desc.findtext('attribute') or 'get'
                current   = desc.findtext('value')
                enum_text = desc.findtext('enum') or ''
                allowed   = enum_text.split() if enum_text.strip() else []

                if not name:
                    continue

                controls.append({
                    "name":           name,
                    "label":          PROP_LABELS.get(name, name),
                    "access":         "getset" if access == "getset" else "getonly",
                    "current_value":  current,
                    "allowed_values": allowed,
                })

            return jsonify(
                ok=True,
                model=camera_info.get("model", "Unknown"),
                firmware=camera_info.get("firmware", "unknown"),
                controls=controls,
            )
        except Exception as e:
            return jsonify(ok=False, error=str(e)), 200

@app.route("/api/prop_labels", methods=["GET"])
def api_prop_labels():
    """Return the current property label map so the UI always uses up-to-date labels."""
    return jsonify(labels=PROP_LABELS)

@app.route("/api/ontology", methods=["GET"])
def api_ontology():
    """Return the property ontology (groups, labels, default expanded state)."""
    return jsonify(ontology=ONTOLOGY)

# ── CHEATS LIBRARY ────────────────────────────────────────────────────────────

ORDER_FILE = CHEATS_DIR / ".order.json"

def load_order():
    """Load custom order from file. Returns list of cheat IDs or None if not set."""
    try:
        if ORDER_FILE.exists():
            return json.loads(ORDER_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None

def save_order(order):
    """Save custom order to file."""
    ORDER_FILE.write_text(json.dumps(order), encoding="utf-8")

def clear_order():
    """Remove custom order file, reverting to default oldest-first."""
    if ORDER_FILE.exists():
        ORDER_FILE.unlink()

def load_cheat_index():
    cheats = []
    for f in CHEATS_DIR.glob("*.cheat"):
        try:
            meta, controls = read_cheat(f)
            cheats.append({
                "id":             f.stem,
                "filename":       f.name,
                "category":       meta.get("category", ""),
                "description":    meta.get("description", f.stem),
                "notes":          meta.get("notes", ""),
                "camera_model":   meta.get("camera_model", ""),
                "firmware":       meta.get("firmware", ""),
                "captured_at":    meta.get("captured_at", ""),
                "control_count":  len(controls),
                "upload_pending": meta.get("upload_pending", False),
                "uploaded":       meta.get("uploaded", False),
                "mtime":          f.stat().st_mtime,
            })
        except Exception:
            pass

    custom_order = load_order()
    if custom_order:
        # Apply custom order — put ordered items first, append any new ones at end
        order_map = {cid: i for i, cid in enumerate(custom_order)}
        ordered   = sorted(cheats, key=lambda c: order_map.get(c["id"], len(custom_order)))
        is_custom = True
    else:
        # Default: oldest first (ascending mtime)
        ordered   = sorted(cheats, key=lambda c: c["mtime"])
        is_custom = False

    # Remove mtime from output
    for c in ordered:
        c.pop("mtime", None)

    return ordered, is_custom

@app.route("/api/cheats", methods=["GET"])
def api_cheats_list():
    cheats, is_custom = load_cheat_index()
    pending = sum(1 for c in cheats if c.get("upload_pending"))
    return jsonify(cheats=cheats, pending_count=pending, is_custom_order=is_custom)

@app.route("/api/cheats/order", methods=["POST"])
def api_save_order():
    """Save custom drag-drop order."""
    body  = request.get_json(force=True)
    order = body.get("order", [])
    if not isinstance(order, list):
        return jsonify(ok=False, error="Order must be a list"), 400
    save_order(order)
    return jsonify(ok=True)

@app.route("/api/cheats/order", methods=["DELETE"])
def api_reset_order():
    """Reset to default oldest-first order."""
    clear_order()
    return jsonify(ok=True)

@app.route("/api/cheats/<cheat_id>", methods=["GET"])
def api_cheat_detail(cheat_id):
    path = CHEATS_DIR / f"{cheat_id}.cheat"
    if not path.exists():
        return jsonify(ok=False, error="Not found"), 404
    try:
        meta, controls = read_cheat(path)
        return jsonify(ok=True, meta=meta, controls=controls)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500

@app.route("/api/cheats/<cheat_id>", methods=["DELETE"])
def api_cheat_delete(cheat_id):
    safe = all(c.isalnum() or c in "-_" for c in cheat_id)
    if not safe:
        return jsonify(ok=False, error="Invalid ID"), 400
    path = CHEATS_DIR / f"{cheat_id}.cheat"
    if not path.exists():
        return jsonify(ok=False, error="Not found"), 404
    path.unlink()
    return jsonify(ok=True)

# ── IMPORT CHEAT (drag-drop) ──────────────────────────────────────────────────

@app.route("/api/import_cheat", methods=["POST"])
def api_import_cheat():
    """Accept a .cheat file uploaded via drag-drop, validate, and save to Cheats folder."""
    if "file" not in request.files:
        return jsonify(ok=False, error="No file provided"), 400
    f = request.files["file"]
    if not f.filename.endswith(".cheat"):
        return jsonify(ok=False, error="File must have a .cheat extension"), 400
    raw = f.read()
    # Validate magic header
    if len(raw) < 10 or raw[:4] != MAGIC:
        return jsonify(ok=False, error="Not a valid .cheat file"), 400
    # Verify checksum
    try:
        stored_crc   = struct.unpack(">I", raw[-4:])[0]
        computed_crc = zlib.crc32(raw[:-4]) & 0xFFFFFFFF
        if stored_crc != computed_crc:
            return jsonify(ok=False, error="File is corrupted (checksum mismatch)"), 400
    except Exception:
        return jsonify(ok=False, error="Could not validate file"), 400

    # Check for duplicate by comparing CRC32 of raw content against existing files
    file_crc = zlib.crc32(raw) & 0xFFFFFFFF
    for existing in CHEATS_DIR.glob("*.cheat"):
        try:
            existing_raw = existing.read_bytes()
            existing_crc = zlib.crc32(existing_raw) & 0xFFFFFFFF
            if existing_crc == file_crc:
                # Same file already in library
                meta, controls = read_cheat(existing)
                return jsonify(
                    ok=False,
                    duplicate=True,
                    error="Already in library: " + meta.get("description", existing.stem)
                ), 200
        except Exception:
            pass

    # Generate a clean new filename based on content metadata
    try:
        meta, controls = read_cheat(Path("/dev/null"))  # won't work — parse from raw
    except Exception:
        pass

    # Parse meta from raw bytes to generate proper filename
    try:
        pos      = 6
        meta_len = struct.unpack(">I", raw[pos:pos+4])[0]; pos += 4
        meta     = json.loads(raw[pos:pos+meta_len].decode("utf-8"))
        desc     = meta.get("description", "imported")
        slug     = "".join(c if c.isalnum() else "_" for c in desc.lower())[:32]
        ts       = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        dest     = CHEATS_DIR / f"{ts}_{slug}.cheat"
    except Exception:
        ts   = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        dest = CHEATS_DIR / f"{ts}_imported.cheat"

    dest.write_bytes(raw)

    try:
        meta, controls = read_cheat(dest)
        return jsonify(
            ok=True,
            cheat_id=dest.stem,
            description=meta.get("description", dest.stem),
            camera_model=meta.get("camera_model", ""),
        )
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500

# ── SET UPLOAD PENDING ────────────────────────────────────────────────────────

@app.route("/api/cheats/<cheat_id>/set_pending", methods=["POST"])
def api_set_pending(cheat_id):
    """Set or clear the upload_pending flag on a cheat."""
    safe = all(c.isalnum() or c in "-_" for c in cheat_id)
    if not safe:
        return jsonify(ok=False, error="Invalid ID"), 400
    path = CHEATS_DIR / f"{cheat_id}.cheat"
    if not path.exists():
        return jsonify(ok=False, error="Not found"), 404
    body    = request.get_json(force=True)
    pending = bool(body.get("pending", False))
    try:
        update_cheat_meta(path, {"upload_pending": pending})
        return jsonify(ok=True, upload_pending=pending)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500

# ── SYNC STATUS ───────────────────────────────────────────────────────────────

@app.route("/api/sync_status", methods=["GET"])
def api_sync_status():
    pending = sum(
        1 for f in CHEATS_DIR.glob("*.cheat")
        if read_cheat_meta_safe(f).get("upload_pending", False)
    )
    online = check_internet()
    return jsonify(
        ok=True,
        online=online,
        pending=pending,
        last_sync=sync_status.get("last_sync"),
        synced=sync_status.get("synced", 0),
        community_ready=bool(COMMUNITY_UPLOAD_URL),
    )

@app.route("/api/sync_now", methods=["POST"])
def api_sync_now():
    """Trigger an immediate sync attempt."""
    threading.Thread(target=do_sync, daemon=True).start()
    return jsonify(ok=True, message="Sync started")

# ── OPEN CHEATS FOLDER ────────────────────────────────────────────────────────

@app.route("/api/open_cheats_folder", methods=["POST"])
def api_open_cheats_folder():
    """Open the Cheats folder in Windows Explorer, bringing it to the foreground."""
    import subprocess
    try:
        script = f'''
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class Win32 {{
    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")]
    public static extern IntPtr FindWindow(string lpClassName, string lpWindowName);
}}
"@
Start-Process explorer.exe -ArgumentList "{str(CHEATS_DIR)}"
Start-Sleep -Milliseconds 800
$hwnd = [Win32]::FindWindow("CabinetWClass", $null)
if ($hwnd -ne [IntPtr]::Zero) {{
    [Win32]::SetForegroundWindow($hwnd)
}}
'''
        subprocess.Popen(
            ["powershell", "-WindowStyle", "Hidden", "-Command", script],
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        return jsonify(ok=True)
    except Exception as e:
        try:
            os.startfile(str(CHEATS_DIR))
            return jsonify(ok=True)
        except Exception as e2:
            return jsonify(ok=False, error=str(e2)), 500

# ── SAVE CHEAT ────────────────────────────────────────────────────────────────

@app.route("/api/save_cheat", methods=["POST"])
def api_save_cheat():
    body        = request.get_json(force=True)
    category    = body.get("category", "").strip()
    description = body.get("description", "").strip()
    notes       = body.get("notes", "").strip()
    probe_data  = body.get("probe_data", {})

    if not category or not description:
        return jsonify(ok=False, error="Category and description are required"), 400
    if not probe_data or not probe_data.get("controls"):
        return jsonify(ok=False, error="No probe data"), 400

    # Check for duplicate descriptions — auto-suffix with (2), (3) etc.
    existing_descriptions = set()
    for f in CHEATS_DIR.glob("*.cheat"):
        try:
            meta, _ = read_cheat(f)
            existing_descriptions.add(meta.get("description", "").strip().lower())
        except Exception:
            pass

    final_description = description
    if description.lower() in existing_descriptions:
        n = 2
        while f"{description} ({n})".lower() in existing_descriptions:
            n += 1
        final_description = f"{description} ({n})"

    slug     = "".join(c if c.isalnum() else "_" for c in final_description.lower())[:32]
    ts       = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    cheat_id = f"{ts}_{slug}"
    path     = CHEATS_DIR / f"{cheat_id}.cheat"

    meta = {
        "category":      category,
        "description":   final_description,
        "notes":         notes,
        "camera_model":  probe_data.get("model", camera_info.get("model", "Unknown")),
        "firmware":      probe_data.get("firmware", camera_info.get("firmware", "")),
        "captured_at":   datetime.now(timezone.utc).isoformat(),
        "upload_pending": False,
        "uploaded":       False,
    }

    try:
        write_cheat(path, meta, probe_data["controls"])
        return jsonify(ok=True, cheat_id=cheat_id, filename=path.name,
                       description=final_description)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500

@app.route("/api/cheats/<cheat_id>/update_meta", methods=["POST"])
def api_update_cheat_meta(cheat_id):
    """Update metadata fields of an existing cheat without changing controls."""
    safe = all(c.isalnum() or c in "-_" for c in cheat_id)
    if not safe:
        return jsonify(ok=False, error="Invalid ID"), 400
    path = CHEATS_DIR / f"{cheat_id}.cheat"
    if not path.exists():
        return jsonify(ok=False, error="Not found"), 404
    body = request.get_json(force=True)
    updates = {}
    for field in ["category", "description", "notes"]:
        if field in body:
            updates[field] = body[field].strip()
    try:
        update_cheat_meta(path, updates)
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500

@app.route("/api/load_cheat", methods=["POST"])
def api_load_cheat():
    """
    Returns cheat data combined with live camera probe for compatibility analysis.
    Probes the connected camera and compares against the cheat's controls.
    """
    body     = request.get_json(force=True)
    cheat_id = body.get("cheat_id", "")
    path     = CHEATS_DIR / f"{cheat_id}.cheat"

    if not path.exists():
        return jsonify(ok=False, error="Cheat file not found"), 404

    with camera_lock:
        if not camera_client:
            return jsonify(ok=False, error="No camera connected"), 200

        try:
            # Load the cheat
            meta, cheat_controls = read_cheat(path)

            # Probe the connected camera
            camera_client.send_command('switch_cammode', mode='rec', lvqty='0320x0240')
            resp = camera_client.send_command('get_camprop', com='desc', propname='desclist')
            root = ET.fromstring(resp.text)

            # Build camera property map: name -> { current, access, allowed }
            camera_props = {}
            for desc in root.findall('desc'):
                name      = desc.findtext('propname') or ''
                access    = desc.findtext('attribute') or 'get'
                current   = desc.findtext('value')
                enum_text = desc.findtext('enum') or ''
                allowed   = enum_text.split() if enum_text.strip() else []
                if name:
                    camera_props[name] = {
                        "access":  access,
                        "current": current,
                        "allowed": allowed,
                    }

            # Build compatibility report
            ready       = []  # exact match available
            no_match    = []  # property exists but value not available
            not_applicable = []  # property not on target camera

            for ctrl in cheat_controls:
                name  = ctrl.get("name")
                value = ctrl.get("current_value")
                access = ctrl.get("access", "getset")

                # Skip read-only properties — can't set them anyway
                if access == "getonly":
                    continue

                if name not in camera_props:
                    not_applicable.append({
                        "name":          name,
                        "label":         PROP_LABELS.get(name, name),
                        "source_value":  value,
                    })
                else:
                    cam_prop = camera_props[name]
                    allowed  = cam_prop["allowed"]
                    if value in allowed or not allowed:
                        ready.append({
                            "name":          name,
                            "label":         PROP_LABELS.get(name, name),
                            "source_value":  value,
                            "selected":      value,
                            "allowed":       allowed,
                            "camera_current": cam_prop["current"],
                        })
                    else:
                        no_match.append({
                            "name":          name,
                            "label":         PROP_LABELS.get(name, name),
                            "source_value":  value,
                            "selected":      None,  # user must choose
                            "allowed":       allowed,
                            "camera_current": cam_prop["current"],
                        })

            return jsonify(
                ok=True,
                meta=meta,
                camera_model=camera_info.get("model", "Unknown"),
                ready=ready,
                no_match=no_match,
                not_applicable=not_applicable,
            )

        except Exception as e:
            return jsonify(ok=False, error=str(e)), 500

# ── APPLY CHEAT TO CAMERA ─────────────────────────────────────────────────────

@app.route("/api/apply_cheat", methods=["POST"])
def api_apply_cheat():
    body     = request.get_json(force=True)
    cheat_id = body.get("cheat_id", "")
    path     = CHEATS_DIR / f"{cheat_id}.cheat"

    if not path.exists():
        return jsonify(ok=False, error="Cheat file not found"), 404

    with camera_lock:
        if not camera_client:
            return jsonify(ok=False, error="No camera connected"), 200
        try:
            meta, controls = read_cheat(path)
            applied = 0
            skipped = 0
            errors  = []
            for ctrl in controls:
                if ctrl.get("access") == "getonly":
                    skipped += 1
                    continue
                name  = ctrl.get("name")
                value = ctrl.get("current_value")
                if not name or value is None:
                    skipped += 1
                    continue
                try:
                    camera_client.send_command(
                        'set_camprop', com='set',
                        propname=name, value=str(value))
                    applied += 1
                except Exception as e:
                    errors.append(f"{name}: {e}")
                    skipped += 1
            return jsonify(ok=True, applied=applied,
                           skipped=skipped, errors=errors)
        except Exception as e:
            return jsonify(ok=False, error=str(e)), 500

@app.route("/api/apply_settings", methods=["POST"])
def api_apply_settings():
    """Apply a list of {name, value} pairs to the connected camera."""
    body     = request.get_json(force=True)
    settings = body.get("settings", [])

    with camera_lock:
        if not camera_client:
            return jsonify(ok=False, error="No camera connected"), 200
        try:
            applied = 0
            skipped = 0
            errors  = []
            for s in settings:
                name  = s.get("name")
                value = s.get("value")
                if not name or value is None:
                    skipped += 1
                    continue
                try:
                    camera_client.send_command(
                        'set_camprop', com='set',
                        propname=name, value=str(value))
                    applied += 1
                except Exception as e:
                    errors.append(f"{name}: {e}")
                    skipped += 1
            return jsonify(ok=True, applied=applied,
                           skipped=skipped, errors=errors)
        except Exception as e:
            return jsonify(ok=False, error=str(e)), 500

# ── COMPARISON SESSION ────────────────────────────────────────────────────────

compare_session = []

@app.route("/api/compare", methods=["GET"])
def api_compare_get():
    cheats = []
    for cheat_id in compare_session:
        path = CHEATS_DIR / f"{cheat_id}.cheat"
        if not path.exists():
            continue
        try:
            meta, controls = read_cheat(path)
            cheats.append({"id": cheat_id, "meta": meta, "controls": controls})
        except Exception:
            pass
    return jsonify(cheats=cheats)

@app.route("/api/compare/add", methods=["POST"])
def api_compare_add():
    body     = request.get_json(force=True)
    cheat_id = body.get("cheat_id", "").strip()
    if not cheat_id:
        return jsonify(ok=False, error="No cheat_id provided"), 400
    path = CHEATS_DIR / f"{cheat_id}.cheat"
    if not path.exists():
        return jsonify(ok=False, error="Cheat not found"), 404
    # Purge any stale IDs that no longer have a file
    compare_session[:] = [
        cid for cid in compare_session
        if (CHEATS_DIR / f"{cid}.cheat").exists()
    ]
    if cheat_id not in compare_session:
        if len(compare_session) >= 6:
            return jsonify(ok=False,
                error="Maximum 6 Cheats in comparison. Remove one first."), 200
        compare_session.append(cheat_id)
    return jsonify(ok=True, count=len(compare_session))

@app.route("/api/compare/remove", methods=["POST"])
def api_compare_remove():
    body     = request.get_json(force=True)
    cheat_id = body.get("cheat_id", "").strip()
    if cheat_id in compare_session:
        compare_session.remove(cheat_id)
    return jsonify(ok=True, count=len(compare_session))

@app.route("/api/compare/clear", methods=["POST"])
def api_compare_clear():
    compare_session.clear()
    return jsonify(ok=True)

# ── MAIN ──────────────────────────────────────────────────────────────────────

def open_browser():
    import time
    time.sleep(1.2)
    webbrowser.open("http://localhost:5000")

if __name__ == "__main__":
    # Kill any running installed instances so dev server can take port 5000
    import subprocess
    subprocess.call(
        ["taskkill", "/F", "/IM", "olyprobe.exe"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    print("=" * 52)
    print("  OlyProbe — local server")
    print(f"  Cheats folder: {CHEATS_DIR}")
    print()
    print("  Make sure your PC is connected to the")
    print("  camera's WiFi network before connecting.")
    print()
    print("  Opening browser at http://localhost:5000")
    print("  Press Ctrl+C to quit")
    print("=" * 52)
    threading.Thread(target=open_browser, daemon=True).start()
    app.run(host="127.0.0.1", port=5000, debug=False)
