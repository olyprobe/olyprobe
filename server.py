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

from flask import Flask, request, jsonify, send_file
from olympuswifi.camera import OlympusCamera

# ── PATHS ─────────────────────────────────────────────────────────────────────

BASE_DIR   = Path(__file__).parent.resolve()
UI_FILE    = BASE_DIR / "olyprobe-local.html"
CHEATS_DIR = Path.home() / "OlyProbe" / "Cheats"
CHEATS_DIR.mkdir(parents=True, exist_ok=True)

# ── CHEAT FILE FORMAT ─────────────────────────────────────────────────────────
#
# Binary format with magic header for file validation.
# Layout:
#   4 bytes  magic       b"OLPC"
#   2 bytes  version     uint16  currently 1
#   4 bytes  meta_len    uint32  length of JSON metadata block
#   N bytes  meta_json   UTF-8 JSON
#   4 bytes  data_len    uint32  length of JSON controls block
#   N bytes  data_json   UTF-8 JSON
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

# ── CAMERA STATE ──────────────────────────────────────────────────────────────

camera_lock   = threading.Lock()
camera_client = None
camera_info   = {}

# Human-readable labels for known OPC property names
PROP_LABELS = {
    "takemode":           "Shooting Mode",
    "shutspeedvalue":     "Shutter Speed",
    "isospeedvalue":      "ISO Speed",
    "focalvalue":         "Aperture (f-stop)",
    "expcomp":            "Exposure Compensation",
    "drivemode":          "Drive Mode",
    "wbvalue":            "White Balance",
    "colortone":          "Picture Mode",
    "artfilter":          "Art Filter",
    "colorphase":         "Color Phase",
    "imagesize":          "Image Size",
    "imagequality":       "Image Quality",
    "afmode":             "AF Mode",
    "focal35mm":          "Focal Length (35mm equiv)",
    "recview":            "Rec View",
    "remainshots":        "Remaining Shots",
    "batterylevel":       "Battery Level",
    "mediaid":            "Media ID",
    "exposemovie":        "Movie Exposure Mode",
    "qualitymovie":       "Movie Quality",
    "QualityMovie2":      "Movie Quality 2",
    "modeinfo":           "Mode Info",
    "liveviewquality":    "Live View Quality",
    "destination":        "Save Destination",
    "colorspace":         "Color Space",
    "noisefilter":        "Noise Filter",
    "noisereduction":     "Noise Reduction",
    "digitalzoom":        "Digital Zoom",
    "antiflicker":        "Anti-Flicker",
    "afarea":             "AF Area",
    "facedetect":         "Face Detection",
    "eyedetect":          "Eye Detection",
    "bracketmode":        "Bracket Mode",
    "bracketnum":         "Bracket Count",
    "bracketstep":        "Bracket Step",
    "intervaltime":       "Interval Time",
    "intervalnum":        "Interval Count",
    "bulbtime":           "Bulb Timer",
    "bulbtimelimit":      "Bulb Time Limit",
    "livecomposite":      "Live Composite",
    "focusbracket":       "Focus Bracket",
    "hdrshooting":        "HDR Shooting",
    "multiexposure":      "Multi Exposure",
    "pixelshift":         "Pixel Shift",
    "touchactiveframe":   "Touch Active Frame",
    "lowvibtime":         "Anti-Shock Time",
    "digitaltelecon":     "Digital Teleconverter",
    "supermacrozoom":     "Super Macro Zoom",
    "cameradrivemode":    "Camera Drive Mode",
    "SilentTime":         "Silent Mode Time",
    "SilentNoiseReduction": "Silent Noise Reduction",
    "NoiseReductionExposureTime": "Noise Reduction Exposure Time",
    "ValidMediaSlot":     "Active Media Slot",
}

def xml_value(response_text):
    """Extract value from OPC XML response like <get><value>M</value></get>"""
    try:
        root = ET.fromstring(response_text)
        return root.findtext('value')
    except Exception:
        return None

# ── FLASK APP ─────────────────────────────────────────────────────────────────

app = Flask(__name__)

@app.route("/")
def index():
    if UI_FILE.exists():
        return send_file(UI_FILE, max_age=0)
    return "<h2>olyprobe-local.html not found next to server.py</h2>", 404

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
            settable = camera_client.get_settable_propnames_and_values()
            controls = []

            for name, allowed in settable.items():
                current = None
                try:
                    resp    = camera_client.send_command(
                                  'get_camprop', com='get', propname=name)
                    current = xml_value(resp.text)
                except Exception:
                    pass

                controls.append({
                    "name":           name,
                    "label":          PROP_LABELS.get(name, name),
                    "access":         "getset",
                    "current_value":  current,
                    "allowed_values": allowed if isinstance(allowed, list) else [],
                })

            # Read-only properties worth capturing
            readonly_props = [
                "remainshots", "batterylevel", "mediaid",
                "focal35mm", "modeinfo", "ValidMediaSlot"
            ]
            existing_names = {c["name"] for c in controls}
            for name in readonly_props:
                if name in existing_names:
                    continue
                try:
                    resp = camera_client.send_command(
                               'get_camprop', com='get', propname=name)
                    val  = xml_value(resp.text)
                    if val is not None:
                        controls.append({
                            "name":           name,
                            "label":          PROP_LABELS.get(name, name),
                            "access":         "getonly",
                            "current_value":  val,
                            "allowed_values": [],
                        })
                except Exception:
                    pass

            return jsonify(
                ok=True,
                model=camera_info.get("model", "Unknown"),
                firmware=camera_info.get("firmware", "unknown"),
                controls=controls,
            )
        except Exception as e:
            return jsonify(ok=False, error=str(e)), 200

# ── CHEATS LIBRARY ────────────────────────────────────────────────────────────

def load_cheat_index():
    cheats = []
    for f in sorted(CHEATS_DIR.glob("*.cheat"),
                    key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            meta, controls = read_cheat(f)
            cheats.append({
                "id":            f.stem,
                "filename":      f.name,
                "category":      meta.get("category", ""),
                "description":   meta.get("description", f.stem),
                "camera_model":  meta.get("camera_model", ""),
                "firmware":      meta.get("firmware", ""),
                "captured_at":   meta.get("captured_at", ""),
                "control_count": len(controls),
            })
        except Exception:
            pass
    return cheats

@app.route("/api/cheats", methods=["GET"])
def api_cheats_list():
    return jsonify(cheats=load_cheat_index())

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

# ── SAVE CHEAT ────────────────────────────────────────────────────────────────

@app.route("/api/save_cheat", methods=["POST"])
def api_save_cheat():
    body        = request.get_json(force=True)
    category    = body.get("category", "").strip()
    description = body.get("description", "").strip()
    probe_data  = body.get("probe_data", {})

    if not category or not description:
        return jsonify(ok=False, error="Category and description are required"), 400
    if not probe_data or not probe_data.get("controls"):
        return jsonify(ok=False, error="No probe data"), 400

    slug     = "".join(c if c.isalnum() else "_" for c in description.lower())[:32]
    ts       = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    cheat_id = f"{ts}_{slug}"
    path     = CHEATS_DIR / f"{cheat_id}.cheat"

    meta = {
        "category":     category,
        "description":  description,
        "camera_model": probe_data.get("model", camera_info.get("model", "Unknown")),
        "firmware":     probe_data.get("firmware", camera_info.get("firmware", "")),
        "captured_at":  datetime.now(timezone.utc).isoformat(),
    }

    try:
        write_cheat(path, meta, probe_data["controls"])
        return jsonify(ok=True, cheat_id=cheat_id, filename=path.name)
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

# ── UPLOAD TO COMMUNITY ───────────────────────────────────────────────────────

@app.route("/api/upload_cheat", methods=["POST"])
def api_upload_cheat():
    body     = request.get_json(force=True)
    cheat_id = body.get("cheat_id", "")
    path     = CHEATS_DIR / f"{cheat_id}.cheat"
    if not path.exists():
        return jsonify(ok=False, error="Cheat file not found"), 404
    # TODO: POST to community API when backend is implemented
    return jsonify(ok=False,
        error="Community upload not yet implemented"), 200

# ── COMPARISON SESSION ────────────────────────────────────────────────────────

COMPARE_FILE = BASE_DIR / "olyprobe-compare.html"
compare_session = []   # list of cheat_ids in current comparison, max 6

@app.route("/compare")
def compare_page():
    if COMPARE_FILE.exists():
        return send_file(COMPARE_FILE)
    return "<h2>olyprobe-compare.html not found next to server.py</h2>", 404

@app.route("/api/compare", methods=["GET"])
def api_compare_get():
    """Return full cheat data for all cheats in the comparison session."""
    cheats = []
    for cheat_id in compare_session:
        path = CHEATS_DIR / f"{cheat_id}.cheat"
        if not path.exists():
            continue
        try:
            meta, controls = read_cheat(path)
            cheats.append({
                "id":       cheat_id,
                "meta":     meta,
                "controls": controls,
            })
        except Exception:
            pass
    return jsonify(cheats=cheats)

@app.route("/api/compare/add", methods=["POST"])
def api_compare_add():
    """Add a cheat to the comparison session."""
    body     = request.get_json(force=True)
    cheat_id = body.get("cheat_id", "").strip()
    if not cheat_id:
        return jsonify(ok=False, error="No cheat_id provided"), 400
    path = CHEATS_DIR / f"{cheat_id}.cheat"
    if not path.exists():
        return jsonify(ok=False, error="Cheat not found"), 404
    if cheat_id not in compare_session:
        if len(compare_session) >= 6:
            return jsonify(ok=False,
                error="Maximum 6 Cheats in comparison. Remove one first."), 200
        compare_session.append(cheat_id)
    return jsonify(ok=True, count=len(compare_session))

@app.route("/api/compare/remove", methods=["POST"])
def api_compare_remove():
    """Remove a cheat from the comparison session."""
    body     = request.get_json(force=True)
    cheat_id = body.get("cheat_id", "").strip()
    if cheat_id in compare_session:
        compare_session.remove(cheat_id)
    return jsonify(ok=True, count=len(compare_session))

@app.route("/api/compare/clear", methods=["POST"])
def api_compare_clear():
    """Clear all cheats from the comparison session."""
    compare_session.clear()
    return jsonify(ok=True)

# ── MAIN ──────────────────────────────────────────────────────────────────────

def open_browser():
    import time
    time.sleep(1.2)
    webbrowser.open("http://localhost:5000")

if __name__ == "__main__":
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
