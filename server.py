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
    "usb_D0C7":           "Image Quality (USB)",
    "usb_D00D":           "RAW Mode (USB)",
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
            "expcomp", "bulbtimelimit", "wbvalue",
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
            # USB-only IQ properties
            "usb_D0C7", "usb_D00D",
        ]
    },
    {
        "id": "video",
        "label": "Video",
        "default_expanded": False,
        "properties": [
            "exposemovie", "QualityMovie2", "qualitymovie",
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

WBVALUE_LABELS = {
    "0":   "Auto",
    "18":  "Daylight",
    "16":  "Cloudy",
    "17":  "Shade",
    "20":  "Tungsten",
    "35":  "Fluorescent Warm",
    "23":  "Flash",
    "64":  "Underwater",
    "256": "One-Touch WB 1",
    "257": "One-Touch WB 2",
    "258": "One-Touch WB 3",
    "259": "One-Touch WB 4",
    "512": "CWB (Kelvin — verify on camera)",
}

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


# ── USB TETHERING ─────────────────────────────────────────────────────────────

def is_usb_permitted():
    """Permission hook for USB tethering. Returns True during development."""
    return True


class USBCameraProxy:
    """
    Wraps the USB camera bridge to present the same interface
    as OlympusCamera for use in server.py endpoints.
    """
    OLY_GUID = "4D545058-8900-40B3-8F1D-DC246E1E8370"

    WIFI_TO_MTP = {
        "expcomp":        0xD008,
        "shutspeedvalue": 0xD01C,
        "focalvalue":     0xD002,
        "isospeedvalue":  0xD1C0,
        "wbvalue":        0xD01E,
        "colortone":      0xD010,
        "afmode":         0xD003,
        "drivemode":      0xD009,
        "imagequality":   0xD0C7,
        "exposemovie":    0xD08C,
        "flashmode":      0xD005,
        "flashcomp":      0xD00F,
        "meteringmode":   0xD004,
        "subjectdetect":  0xD1D0,
        "highresshot":    0xD1B9,
        "hdr":            0xD0AD,
        "wbcompa":        0xD033,
        "wbcompg":        0xD034,
    }

    USB_LABELS = {
        0xD005: "Flash Mode",
        0xD00F: "Flash Compensation",
        0xD004: "Metering Mode",
        0xD1D0: "Subject Detection",
        0xD1B9: "High Res Shot",
        0xD0AD: "HDR",
        0xD033: "WB Compensation A",
        0xD034: "WB Compensation G",
        0xD00D: "Image Quality",
    }

    USB_ENUMS = {
        0xD009: {0x01:"Single frame",0x07:"Single frame silent",
                 0x21:"Sequential",0x27:"Silent sequential",
                 0x28:"High speed sequential 1",0x29:"High speed sequential 2",
                 0x48:"Pro Cap SH1",0x49:"Pro Cap SH2",
                 0x04:"Self-timer 12s",0x05:"Self-timer 2s",
                 0x24:"Silent self-timer 2s",0x06:"Custom self-timer"},
        0xD004: {0x8001:"Digital ESP",0x0002:"Center weighted",
                 0x0004:"Spot",0x8011:"Spot highlight",0x8012:"Spot shadow"},
        0xD1D0: {1:"Off",2:"Human",3:"Motorsports",4:"Airplanes",
                 5:"Trains",6:"Birds",7:"Dogs and cats"},
        0xD1B9: {1:"Off",2:"On tripod",3:"On handheld"},
        0xD003: {0x0001:"MF",0x0002:"S-AF",0x8002:"C-AF",
                 0x8004:"Preset MF",0x8007:"Starry Sky AF"},
        0xD005: {1:"Auto",2:"Off",3:"On/Fill",4:"Red-eye",5:"Slow sync",
                 6:"Slow sync red-eye"},
        0xD01E: {1:"Auto",2:"Sunny",3:"Shade",4:"Cloudy",5:"Incandescent",
                 6:"Fluorescent",7:"Underwater",8:"WB Flash",
                 9:"One-Touch WB 1",10:"One-Touch WB 2",
                 11:"One-Touch WB 3",12:"One-Touch WB 4",13:"Custom WB"},
        0xD010: {0x8301:"Vivid",0x8302:"Natural",0x8303:"Muted",
                 0x8304:"Portrait",0x8305:"Landscape",0x8306:"Flat",
                 0x8307:"Monotone",0x8611:"e-Portrait",
                 0x0002:"Natural",0x0001:"Vivid"},
        0xD08C: {1:"P",2:"A",3:"S",4:"M"},
        0xD0AD: {1:"Off",2:"HDR1",3:"HDR2",4:"Auto HDR"},
        0xD00D: {0x0000:"Off",0x0020:"RAW",0x0021:"RAW+Large Fine",
                 0x0022:"RAW+Large Normal",0x0023:"RAW+Medium Fine"},
        0xD0C7: {0x0107:"Large Fine",0x0106:"Large Normal",
                 0x0105:"Large Basic",0x0207:"Medium Fine",
                 0x0206:"Medium Normal",0x0307:"Small Fine",
                 0x0306:"Small Normal",0x0305:"Small Basic",
                 0x0128:"RAW+Large Fine",0x0120:"RAW",
                 296:"RAW+Large Fine",288:"RAW",
                 263:"Large Fine",262:"Large Normal",261:"Large Basic"},
    }

    def __init__(self, wpd_id):
        from usb_camera import _bridge
        self._bridge  = _bridge
        self._wpd_id  = wpd_id
        self.model    = "OM SYSTEM Camera"
        self.firmware = "USB"
        self._method  = "usb"
        # Session cache for physical-dial properties that WPD can't read live
        # Keyed by mtp_code, value is the decoded string
        self._physical_cache = {}

    def probe(self):
        """Return controls in same format as WiFi probe."""
        import struct
        r = self._bridge(["get"])
        if not r.get("ok"):
            raise Exception(r.get("error", "USB probe failed"))
        oly_props = {p['pid']: p for p in r.get('props', [])
                     if p['guid'].lower() == self.OLY_GUID.lower()}

        # Fetch real aperture allowed values from camera (lens-dependent)
        aperture_allowed = []
        try:
            r_ap = self._bridge(["getallowed", f"{0xD002:04X}"])
            if r_ap.get("ok") and r_ap.get("values"):
                # Filter to realistic lens range: f/1.0 to f/22 (values 10-220)
                # Full body range includes f/91 etc. which no lens supports
                aperture_allowed = [
                    f"f/{v/10:.1f}" for v in r_ap["values"]
                    if 10 <= v <= 220 and v > 0
                ]
        except Exception:
            pass
        raw_mode_prop = oly_props.get(0xD00D)
        raw_mode_val  = 0
        if raw_mode_prop:
            raw_mode_val = struct.unpack_from('<H',
                bytes.fromhex(raw_mode_prop['val']), 0)[0]

        # Properties with physical dials — MTP reads stale/zero values
        # Can be written when applying a cheat, but current value is unreliable
        PHYSICAL_PROPS = {0xD01C, 0xD002, 0xD008}

        controls = []
        for wifi_name, mtp_code in self.WIFI_TO_MTP.items():
            if mtp_code not in oly_props:
                continue
            prop  = oly_props[mtp_code]
            raw   = bytes.fromhex(prop['val'])

            # Physical control properties — WPD cache is always 0/stale
            # Use session cache if available (populated by set_property)
            if mtp_code in PHYSICAL_PROPS:
                val = self._physical_cache.get(mtp_code, None)
            # For IQ, combine D0C7 and D00D
            elif mtp_code == 0xD0C7:
                if raw_mode_val == 0x0020:
                    val = "RAW"
                elif raw_mode_val in (0x0021, 0x0128):
                    val = "RAW+Large Fine"
                else:
                    val = self._decode(mtp_code, raw)
            else:
                val = self._decode(mtp_code, raw)

            label = PROP_LABELS.get(wifi_name) or self.USB_LABELS.get(mtp_code, wifi_name)
            enums = self.USB_ENUMS.get(mtp_code, {})
            # For allowed values, use unique values only
            allowed = list(dict.fromkeys(enums.values())) if enums else []
            # For continuous range properties, provide common values as hints
            # Note: aperture uses real values from camera descriptor (lens-dependent)
            RANGE_HINTS = {
                0xD008: ["-5.0","-4.0","-3.0","-2.0","-1.0","-0.7","-0.3",
                         "0.0","+0.3","+0.7","+1.0","+2.0","+3.0","+4.0","+5.0"],
                0xD01C: ["1/8000","1/4000","1/2000","1/1000","1/500","1/250",
                         "1/125","1/60","1/30","1/15","1/8","1/4","1/2",
                         "1\"","2\"","4\"","8\"","15\"","30\"","60\""],
                0xD1C0: ["Auto","100","200","400","800","1600","3200",
                         "6400","12800","25600","51200","102400"],
                0xD00F: ["-3.0","-2.0","-1.0","0.0","+1.0","+2.0","+3.0"],
                0xD002: aperture_allowed,  # real lens values from camera
            }
            if not allowed and mtp_code in RANGE_HINTS:
                allowed = RANGE_HINTS[mtp_code]

            controls.append({
                "name":           wifi_name if wifi_name in PROP_LABELS else f"usb_{mtp_code:04X}",
                "label":          label,
                "access":         "getset",
                "current_value":  val,
                "allowed_values": allowed,
                "mtp_code":       mtp_code,
            })
        # Shooting mode from WiFi desclist — read-only via USB (physical dial)
        # Add takemode as read-only if present in cheat
        # USB doesn't have takemode in MTP properties — skip it
        # It will appear as unclassified if present in WiFi-sourced cheats

        return controls

    def set_property(self, name, value_str):
        """Set a property by WiFi name."""
        import struct
        if name.startswith("usb_"):
            mtp_code = int(name[4:], 16)
        else:
            mtp_code = self.WIFI_TO_MTP.get(name)
        if mtp_code is None:
            raise ValueError(f"Unknown property: {name}")
        raw = self._encode(mtp_code, value_str)
        if raw is None:
            raise ValueError(f"Cannot encode '{value_str}' for 0x{mtp_code:04X}")
        while len(raw) < 4:
            raw = raw + b'\x00'
        r = self._bridge(["setprop", f"{mtp_code:04X}", raw.hex()])
        if not r.get("ok"):
            raise Exception(r.get("error", "Set failed"))
        # Update session cache so probe() can show the value we just wrote
        self._physical_cache[mtp_code] = value_str
        return True

    def _decode(self, mtp_code, raw):
        import struct
        if mtp_code == 0xD01C:  # Shutter speed: bytes[0:2]=denom, bytes[2:4]=numer
            if len(raw) >= 4:
                denom = struct.unpack_from('<H', raw, 0)[0]
                numer = struct.unpack_from('<H', raw, 2)[0]
                if denom == 0:
                    return "Bulb"
                if numer <= 1:
                    return f"1/{denom}"
                # numer > 1: slow speed, display as fraction of seconds
                # e.g. denom=10, numer=60 → 60/10 = 6 seconds
                secs = numer / denom
                if secs == int(secs):
                    return f"{int(secs)}\""
                return f"{secs:.1f}\""
        if mtp_code in (0xD008, 0xD00F):  # milliEV
            if len(raw) >= 2:
                val = struct.unpack_from('<h', raw, 0)[0]
                if abs(val) <= 5:  # treat ±5 milliEV as zero
                    return "0.0"
                ev = val / 1000
                return f"{ev:+.1f}"
        if mtp_code == 0xD002:  # Aperture x10
            if len(raw) >= 2:
                val = struct.unpack_from('<H', raw, 0)[0]
                return f"f/{val/10:.1f}" if val > 0 else "f/--"
        if mtp_code == 0xD1C0:  # ISO
            if len(raw) >= 4:
                val = struct.unpack_from('<I', raw, 0)[0]
                return "Auto" if val in (0, 0xFFFFFFFF) else str(val)
        if mtp_code in (0xD033, 0xD034):  # WB comp
            if len(raw) >= 2:
                val = struct.unpack_from('<h', raw, 0)[0]
                return f"{'A' if mtp_code==0xD033 else 'G'}{val:+d}"
        if mtp_code in self.USB_ENUMS:
            if len(raw) >= 2:
                val = struct.unpack_from('<H', raw, 0)[0]
                return self.USB_ENUMS[mtp_code].get(val, str(val))
        if len(raw) >= 2:
            return str(struct.unpack_from('<H', raw, 0)[0])
        return raw.hex()

    def _encode(self, mtp_code, value_str):
        import struct
        if mtp_code == 0xD01C:
            if '/' in value_str:
                parts = value_str.split('/')
                return struct.pack('<HH', int(parts[0]), int(parts[1]))
        if mtp_code in (0xD008, 0xD00F):
            s = value_str.replace('+','').replace(' EV','').strip()
            return struct.pack('<h', int(float(s)*1000))
        if mtp_code == 0xD002:
            return struct.pack('<H', int(float(value_str.replace('f/','').strip())*10))
        if mtp_code == 0xD1C0:
            val = 0xFFFFFFFF if value_str == "Auto" else int(value_str)
            return struct.pack('<I', val)
        if mtp_code in (0xD033, 0xD034):
            s = value_str[1:] if value_str and value_str[0] in 'AG' else value_str
            return struct.pack('<h', int(s))
        if mtp_code in self.USB_ENUMS:
            rev = {v: k for k, v in self.USB_ENUMS[mtp_code].items()}
            if value_str in rev:
                return struct.pack('<H', rev[value_str])
            try: return struct.pack('<H', int(value_str))
            except ValueError: return None
        try:
            val = int(value_str)
            return struct.pack('<H', val) if val < 65536 else struct.pack('<I', val)
        except ValueError:
            return None

    def send_command(self, cmd, **kwargs):
        raise NotImplementedError(f"WiFi command '{cmd}' not supported over USB")


# ── AUTO-DETECT ───────────────────────────────────────────────────────────────

@app.route("/api/detect", methods=["GET"])
def api_detect():
    """
    Quick non-connecting scan to see what cameras are available.
    Returns: {usb: bool, wifi: bool}
    USB check is fast (~1s). WiFi check just pings 192.168.0.10.
    """
    usb_found  = False
    wifi_found = False

    # USB check
    try:
        from usb_camera import _bridge
        r = _bridge(["list"])
        usb_found = bool(r.get("ok") and r.get("device"))
    except Exception:
        pass

    # WiFi check — quick TCP ping to port 80 on camera IP
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.5)
        result = s.connect_ex(("192.168.0.10", 80))
        s.close()
        wifi_found = (result == 0)
    except Exception:
        pass

    return jsonify(usb=usb_found, wifi=wifi_found)


@app.route("/api/heartbeat", methods=["GET"])
def api_heartbeat():
    """
    Lightweight connection health check. Returns ok=True if camera is
    still responding, ok=False if connection has dropped.
    """
    with camera_lock:
        if not camera_client:
            return jsonify(ok=False, reason="not_connected")
        try:
            if isinstance(camera_client, USBCameraProxy):
                # Read ISO — fast single-property read
                from usb_camera import _bridge
                r = _bridge(["getprop", f"{0xD1C0:04X}"])
                if not r.get("ok"):
                    return jsonify(ok=False, reason="usb_read_failed")
            else:
                # WiFi: send a cheap get_caminfo
                camera_client.send_command('get_caminfo')
            return jsonify(ok=True)
        except Exception as e:
            return jsonify(ok=False, reason=str(e))


def wifi_set_camprop(cam, propname, value):
    """
    Set a camera property via WiFi OPC using the correct POST format.
    """
    import requests
    # Normalize expcomp — camera expects integer-like strings e.g. "0", "+3", "-3"
    # not "0.0" or "+1.0" style from USB cheats
    if propname == 'expcomp':
        try:
            fv = float(value.replace('+',''))
            if fv == 0:
                value = '0'
            elif fv == int(fv):
                value = f"{int(fv):+d}" if fv != 0 else '0'
            # else leave as-is (camera's own enum values)
        except (ValueError, AttributeError):
            pass

    url = "http://192.168.0.10/set_camprop.cgi"
    params = {'com': 'set', 'propname': propname}
    xml_body = f'<?xml version="1.0"?><set><value>{value}</value></set>'.encode('utf-8')
    headers = {
        'Host': '192.168.0.10',
        'User-Agent': 'OlympusCameraKit',
        'Content-Type': 'text/plain;charset=utf-8',
    }
    print(f"[wifi_set] {propname}={value}", flush=True)
    r = requests.post(url, params=params, data=xml_body, headers=headers, timeout=10)
    print(f"[wifi_set] status={r.status_code} body={r.text[:200]}", flush=True)
    if r.status_code not in (200, 202):
        raise Exception(f"set_camprop failed: HTTP {r.status_code}: {r.text[:100]}")
    return r


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
            if not is_usb_permitted():
                return jsonify(ok=False, error="USB tethering requires a premium subscription"), 200
            try:
                from usb_camera import _bridge
                r = _bridge(["list"])
                if not r.get("ok") or not r.get("device"):
                    return jsonify(ok=False, error="No camera found. Connect via USB in Raw/Control mode."), 200
                wpd_id = r["device"]
                cam = USBCameraProxy(wpd_id)
                # Use the friendly name from WPD if available, normalize spacing
                if r.get("model"):
                    name = r["model"].strip()
                    # Fix common WPD name issues: "OM-1MarkII" → "OM-1 Mark II"
                    import re
                    name = re.sub(r'(OM-\d+(?:Mark|mk))', lambda m: m.group(0), name)
                    name = re.sub(r'([a-z])([A-Z])', r'\1 \2', name)  # camelCase → spaced
                    name = re.sub(r'(Mark)(\w)', r'\1 \2', name)      # MarkII → Mark II
                    cam.model = name
                camera_client = cam
                camera_info   = {"model": cam.model, "firmware": cam.firmware, "method": "usb"}
                return jsonify(ok=True, model=cam.model, firmware=cam.firmware)
            except Exception as e:
                return jsonify(ok=False, error=str(e)), 200

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
                if not isinstance(camera_client, USBCameraProxy):
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
            # USB camera probe
            if isinstance(camera_client, USBCameraProxy):
                controls = camera_client.probe()
                return jsonify(
                    ok=True,
                    controls=controls,
                    model=camera_info.get("model", "Unknown"),
                    firmware=camera_info.get("firmware", ""),
                    method="usb"
                )

            # WiFi camera probe
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
                    "value_labels":   WBVALUE_LABELS if name == "wbvalue" else None,
                })
            seen = set()
            controls = [c for c in controls if not (c['name'] in seen or seen.add(c['name']))]

            # Fetch last shot EXIF for exposure triangle (SS, aperture, expcomp, WB)
            exif_values = {}
            try:
                import io as _io
                from PIL import Image
                from PIL.ExifTags import TAGS

                # getlastjpg returns the last captured JPEG directly
                jpg_resp = camera_client.send_command('exec_takemisc', com='getlastjpg')
                img = Image.open(_io.BytesIO(jpg_resp.content))
                exif_raw = img._getexif() or {}
                exif = {TAGS.get(k, k): v for k, v in exif_raw.items()}

                def rational_to_str(v):
                    try:
                        if hasattr(v, 'numerator'):
                            return float(v.numerator) / float(v.denominator)
                        if isinstance(v, tuple) and len(v) == 2:
                            return float(v[0]) / float(v[1]) if v[1] else 0.0
                        return float(v)
                    except Exception:
                        return 0.0

                # Shutter speed → OPC format (e.g. "1/250" or "2")
                et = exif.get('ExposureTime')
                if et is not None:
                    s = rational_to_str(et)
                    if s > 0:
                        if s < 1:
                            exif_values['shutspeedvalue'] = f"1/{round(1/s)}"
                        elif s == int(s):
                            exif_values['shutspeedvalue'] = str(int(s))
                        else:
                            exif_values['shutspeedvalue'] = f"{s:.1f}"

                # Aperture → OPC format matching camera values (e.g. "1.6", "8.0", "22")
                fn = exif.get('FNumber')
                if fn is not None:
                    fnum = rational_to_str(fn)
                    if fnum > 0:
                        # Camera OPC uses one decimal for sub-10 values, integer for 10+
                        if fnum < 10:
                            exif_values['focalvalue'] = f"{fnum:.1f}"
                        else:
                            exif_values['focalvalue'] = str(int(round(fnum)))

                # Exposure compensation → OPC format (e.g. "0", "+3", "-3")
                eb = exif.get('ExposureBiasValue')
                if eb is not None:
                    ev = rational_to_str(eb)
                    if abs(ev) < 0.01:
                        exif_values['expcomp'] = "0"
                    else:
                        # OPC uses integer thirds: 0.3EV steps encoded as integers
                        # Convert to thirds and round
                        thirds = round(ev / (1/3))
                        exif_values['expcomp'] = f"{thirds:+d}" if thirds != 0 else "0"

            except Exception as ex_jpg:
                print(f"[getlastjpg] failed: {ex_jpg}", flush=True)

            # Merge EXIF values into controls — override current_value for exposure triangle
            if exif_values:
                for ctrl in controls:
                    if ctrl['name'] in exif_values:
                        ctrl['current_value'] = exif_values[ctrl['name']]

            return jsonify(
                ok=True,
                model=camera_info.get("model", "Unknown"),
                firmware=camera_info.get("firmware", "unknown"),
                controls=controls,
                exif_source=bool(exif_values),
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
        # Default: newest first (descending mtime)
        ordered   = sorted(cheats, key=lambda c: c["mtime"], reverse=True)
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

        # Inject IQ controls if not already present — allows WiFi cheats to
        # have IQ set manually in View overlay before saving/loading via USB
        existing_names = {c["name"] for c in controls}

        IQ_CONTROLS = [
            {
                "name":           "usb_D0C7",
                "label":          "Image Quality",
                "access":         "getset",
                "current_value":  None,
                "allowed_values": [
                    "Large Fine", "Large Normal", "Large Basic",
                    "Medium Fine", "Medium Normal",
                    "Small Fine", "Small Normal", "Small Basic",
                    "RAW", "RAW+Large Fine", "RAW+Large Normal",
                ],
                "mtp_code":       0xD0C7,
                "value_labels":   None,
                "injected":       True,
            },
        ]

        for ctrl in IQ_CONTROLS:
            if ctrl["name"] not in existing_names:
                controls.append(ctrl)

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
        dest     = CHEATS_DIR / f"{slug}_{ts}.cheat"
    except Exception:
        ts   = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        dest = CHEATS_DIR / f"imported_{ts}.cheat"

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

# ── IMPORT JPEG → CHEAT ──────────────────────────────────────────────────────

@app.route("/api/import_jpeg", methods=["POST"])
def api_import_jpeg():
    """Accept a JPEG, read its EXIF data, and save as a .cheat file."""
    if "file" not in request.files:
        return jsonify(ok=False, error="No file provided"), 400
    f = request.files["file"]
    filename = f.filename or "untitled.jpg"
    if not filename.lower().endswith((".jpg", ".jpeg")):
        return jsonify(ok=False, error="File must be a JPEG"), 400

    try:
        from PIL import Image
        from PIL.ExifTags import TAGS
        import io

        raw_bytes = f.read()
        img = Image.open(io.BytesIO(raw_bytes))
        exif_raw = img._getexif() or {}

        # Decode all EXIF tags
        exif = {}
        for tag_id, value in exif_raw.items():
            tag = TAGS.get(tag_id, tag_id)
            exif[tag] = value

        # ── Extract standard cross-brand EXIF values ──────────────────────────

        def rational_to_float(v):
            """Convert EXIF rational (tuple or IFDRational) to float."""
            try:
                if hasattr(v, 'numerator'):
                    return float(v.numerator) / float(v.denominator)
                if isinstance(v, tuple) and len(v) == 2:
                    return float(v[0]) / float(v[1]) if v[1] else 0.0
                return float(v)
            except Exception:
                return 0.0

        # Shutter speed
        shutter_val = exif.get("ExposureTime")
        shutter_str = None
        if shutter_val is not None:
            s = rational_to_float(shutter_val)
            if s > 0:
                if s < 1:
                    denom = round(1 / s)
                    shutter_str = f"1/{denom}"
                elif s == int(s):
                    shutter_str = f'{int(s)}"'
                else:
                    shutter_str = f'{s:.1f}"'

        # Aperture
        aperture_val = exif.get("FNumber")
        aperture_str = None
        if aperture_val is not None:
            fnum = rational_to_float(aperture_val)
            if fnum > 0:
                aperture_str = f"f/{fnum:.1f}"

        # ISO
        iso_val = exif.get("ISOSpeedRatings")
        iso_str = str(iso_val) if iso_val else None

        # Exposure compensation
        expcomp_val = exif.get("ExposureBiasValue")
        expcomp_str = None
        if expcomp_val is not None:
            ev = rational_to_float(expcomp_val)
            if abs(ev) < 0.01:
                expcomp_str = "0.0"
            else:
                expcomp_str = f"{ev:+.1f}"

        # Exposure program → shooting mode hint
        exp_program = exif.get("ExposureProgram", 0)
        program_map = {0:"Unknown", 1:"Manual", 2:"P", 3:"A", 4:"S",
                       5:"Creative", 6:"Action", 7:"Portrait", 8:"Landscape"}
        program_str = program_map.get(exp_program, "Unknown")

        # Metering mode
        metering = exif.get("MeteringMode", 0)
        metering_map = {0:"Unknown", 1:"Average", 2:"Center weighted",
                        3:"Spot", 4:"Multi-spot", 5:"Pattern", 6:"Partial"}
        metering_str = metering_map.get(metering, "Unknown")

        # White balance (auto/manual only from standard EXIF)
        wb = exif.get("WhiteBalance", 0)
        wb_str = "Auto" if wb == 0 else "Manual"

        # Flash
        flash = exif.get("Flash", 0)
        flash_str = "Off" if (flash & 0x1) == 0 else "On"

        # Focal length
        fl_val = exif.get("FocalLength")
        fl_str = None
        if fl_val is not None:
            fl = rational_to_float(fl_val)
            if fl > 0:
                fl_str = f"{fl:.0f}mm"

        fl35_val = exif.get("FocalLengthIn35mmFilm")
        fl35_str = f"{fl35_val}mm equiv." if fl35_val else None

        # Camera info
        make  = exif.get("Make", "").strip()
        model = exif.get("Model", "").strip()
        camera_str = f"{make} {model}".strip() or "Unknown camera"

        capture_date = exif.get("DateTimeOriginal") or exif.get("DateTime") or ""

        # Lens
        lens_str = exif.get("LensModel", "").strip() or None

        # ── Build controls list ───────────────────────────────────────────────

        # Standard shutter speed allowed values
        ss_allowed = ["1/8000","1/6400","1/5000","1/4000","1/3200","1/2500",
                      "1/2000","1/1600","1/1250","1/1000","1/800","1/640",
                      "1/500","1/400","1/320","1/250","1/200","1/160",
                      "1/125","1/100","1/80","1/60","1/50","1/40","1/30",
                      "1/25","1/20","1/15","1/13","1/10","1/8","1/6","1/5",
                      "1/4","0.3\"","0.4\"","0.5\"","0.6\"","0.8\"",
                      "1\"","1.3\"","1.6\"","2\"","2.5\"","3\"","4\"",
                      "5\"","6\"","8\"","10\"","13\"","15\"","20\"",
                      "25\"","30\"","60\""]
        # Aperture allowed values f/1.0–f/22
        ap_allowed = ["f/1.0","f/1.1","f/1.2","f/1.4","f/1.6","f/1.8",
                      "f/2.0","f/2.2","f/2.5","f/2.8","f/3.2","f/3.5",
                      "f/4.0","f/4.5","f/5.0","f/5.6","f/6.3","f/7.1",
                      "f/8.0","f/9.0","f/10.0","f/11.0","f/13.0","f/14.0",
                      "f/16.0","f/18.0","f/20.0","f/22.0"]
        ec_allowed = ["-5.0","-4.0","-3.0","-2.0","-1.7","-1.3","-1.0",
                      "-0.7","-0.3","0.0","+0.3","+0.7","+1.0","+1.3",
                      "+1.7","+2.0","+3.0","+4.0","+5.0"]
        iso_allowed = ["Auto","100","125","160","200","250","320","400",
                       "500","640","800","1000","1250","1600","2000","2500",
                       "3200","4000","5000","6400","12800","25600","51200","102400"]

        def make_ctrl(name, label, current, allowed, access="getset"):
            return {
                "name": name, "label": label, "access": access,
                "current_value": current, "allowed_values": allowed,
                "mtp_code": None, "source": "exif"
            }

        controls = [
            make_ctrl("shutspeedvalue", "Shutter Speed",        shutter_str,  ss_allowed),
            make_ctrl("focalvalue",     "Aperture (f-stop)",    aperture_str, ap_allowed),
            make_ctrl("isospeedvalue",  "ISO Speed",            iso_str,      iso_allowed),
            make_ctrl("expcomp",        "Exposure Compensation",expcomp_str,  ec_allowed),
            make_ctrl("wbvalue",        "White Balance",        wb_str,       ["Auto","Manual"], "getonly"),
            make_ctrl("usb_D004",       "Metering Mode",        metering_str, list(metering_map.values()), "getonly"),
        ]
        # Append read-only info fields
        if program_str != "Unknown":
            controls.append(make_ctrl("takemode", "Exposure Program", program_str, [], "getonly"))
        if flash_str:
            controls.append(make_ctrl("usb_D005", "Flash", flash_str, ["Off","On/Fill"], "getonly"))
        if fl_str:
            controls.append(make_ctrl("focallength", "Focal Length", fl_str, [], "getonly"))
        if fl35_str:
            controls.append(make_ctrl("focallength35", "35mm Equivalent", fl35_str, [], "getonly"))
        if lens_str:
            controls.append(make_ctrl("lens", "Lens", lens_str, [], "getonly"))

        # ── Build cheat name from filename ────────────────────────────────────
        stem = Path(filename).stem  # filename without extension
        notes_parts = [f"Camera: {camera_str}"]
        if capture_date:
            notes_parts.append(f"Captured: {capture_date}")
        if lens_str:
            notes_parts.append(f"Lens: {lens_str}")
        notes = "\n".join(notes_parts)

        # ── Save cheat ────────────────────────────────────────────────────────
        probe_data = {
            "model": camera_str,
            "firmware": "EXIF import",
            "controls": controls,
        }
        cheat_id, desc = _save_cheat(
            category="Other",
            description=stem,
            notes=notes,
            probe_data=probe_data,
        )
        return jsonify(ok=True, cheat_id=cheat_id, description=desc,
                       camera=camera_str, properties=len(controls))

    except ImportError:
        return jsonify(ok=False, error="Pillow library not installed. Run: pip install Pillow"), 500
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
    """Open the Cheats folder in Windows Explorer."""
    import subprocess
    try:
        subprocess.Popen(["explorer.exe", str(CHEATS_DIR)])
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500

# ── SAVE CHEAT ────────────────────────────────────────────────────────────────


def _save_cheat(category, description, notes, probe_data):
    """Core save logic — returns (cheat_id, final_description)."""
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
    cheat_id = f"{slug}_{ts}"
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
    write_cheat(path, meta, probe_data["controls"])
    return cheat_id, final_description


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

    try:
        cheat_id, final_description = _save_cheat(category, description, notes, probe_data)
        return jsonify(ok=True, cheat_id=cheat_id, description=final_description)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500

@app.route("/api/cheats/<cheat_id>/update_meta", methods=["POST"])
def api_update_cheat_meta(cheat_id):
    """Update metadata and optionally controls of an existing cheat."""
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
        if "controls" in body:
            # Rewrite entire cheat with updated controls
            meta, _ = read_cheat(path)
            meta.update(updates)
            # Strip injected flag before writing — keep all controls including unset ones
            clean_controls = [
                {k: v for k, v in ctrl.items() if k != "injected"}
                for ctrl in body["controls"]
            ]
            write_cheat(path, meta, clean_controls)
        else:
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
            if isinstance(camera_client, USBCameraProxy):
                # USB camera probe
                usb_controls = camera_client.probe()
                camera_props = {}
                for ctrl in usb_controls:
                    name = ctrl.get("name")
                    if name:
                        camera_props[name] = {
                            "access":  "getset",
                            "current": ctrl.get("current_value"),
                            "allowed": ctrl.get("allowed_values", []),
                        }
            else:
                # WiFi camera probe
                camera_client.send_command('switch_cammode', mode='rec', lvqty='0320x0240')
                resp = camera_client.send_command('get_camprop', com='desc', propname='desclist')
                root = ET.fromstring(resp.text)
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
                # takemode is set by physical dial, treat as read-only
                if access == "getonly" or name == "takemode":
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

            is_usb = isinstance(camera_client, USBCameraProxy)
            if not is_usb:
                camera_client.send_command('switch_cammode', mode='rec', lvqty='0320x0240')

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
                    if is_usb:
                        camera_client.set_property(name, str(value))
                    else:
                        wifi_set_camprop(camera_client, name, value)
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

            if isinstance(camera_client, USBCameraProxy):
                # USB camera
                for s in settings:
                    name  = s.get("name")
                    value = s.get("value")
                    if not name or value is None:
                        skipped += 1
                        continue
                    # Skip read-only properties
                    if name in ("takemode",):
                        skipped += 1
                        continue
                    try:
                        camera_client.set_property(name, str(value))
                        applied += 1
                    except Exception as e:
                        errors.append(f"{name}: {e}")
                        skipped += 1
            else:
                # WiFi camera — ensure in rec mode first
                camera_client.send_command('switch_cammode', mode='rec', lvqty='0320x0240')
                for s in settings:
                    name  = s.get("name")
                    value = s.get("value")
                    if not name or value is None:
                        skipped += 1
                        continue
                    try:
                        wifi_set_camprop(camera_client, name, value)
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
