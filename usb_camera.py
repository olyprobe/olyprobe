"""
usb_camera.py — Olympus/OM SYSTEM USB tethering via Windows WPD

Uses WPD object model: IPortableDevice -> Content -> Properties -> GetValues
Driver:   Standard Windows MTP driver (NOT Zadig/libusb)
Camera:   Must be in Raw/Control USB mode
"""

import struct
import subprocess
import json
import os
import tempfile
import glob
import shutil

def _purge_old_bridges():
    pattern = os.path.join(tempfile.gettempdir(), "olyprobe_*")
    for path in glob.glob(pattern):
        try:
            shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass

_purge_old_bridges()

# ── PERMISSION HOOK ───────────────────────────────────────────────────────────

def is_usb_permitted():
    return True

# ── PROPERTY MAP AND ENUMS ────────────────────────────────────────────────────

PROP_MAP = {
    0xD008: {"name": "Exposure Compensation",  "wifi": "expcomp"},
    0xD01C: {"name": "Shutter Speed",          "wifi": "shutspeedvalue"},
    0xD002: {"name": "Aperture",               "wifi": "focalvalue"},
    0xD1C0: {"name": "ISO Speed",              "wifi": "isospeedvalue"},
    0xD01E: {"name": "White Balance Type",     "wifi": "wbvalue"},
    0xD033: {"name": "WB Compensation A-axis", "wifi": None},
    0xD034: {"name": "WB Compensation G-axis", "wifi": None},
    0xD010: {"name": "Picture Mode",           "wifi": "colortone"},
    0xD005: {"name": "Flash Mode",             "wifi": None},
    0xD00F: {"name": "Flash Compensation",     "wifi": None},
    0xD003: {"name": "AF Mode",                "wifi": "afmode"},
    0xD009: {"name": "Drive Mode",             "wifi": "drivemode"},
    0xD1B9: {"name": "High Res Shot",          "wifi": None},
    0xD004: {"name": "Metering Mode",          "wifi": None},
    0xD1D0: {"name": "Subject Detection",      "wifi": None},
    0xD0C7: {"name": "Image Quality",          "wifi": "imagequality"},
    0xD0AD: {"name": "HDR",                    "wifi": None},
    0xD08C: {"name": "Movie Exposure Mode",    "wifi": "exposemovie"},
}

PROP_VALUES = {
    0xD01E: {1:"Auto",2:"Sunny",3:"Shade",4:"Cloudy",5:"Incandescent",
             6:"Fluorescent",7:"Underwater",8:"WB Flash",
             9:"One-Touch WB 1",10:"One-Touch WB 2",
             11:"One-Touch WB 3",12:"One-Touch WB 4",13:"Custom WB"},
    0xD009: {0x01:"Single frame",0x07:"Single frame silent",
             0x21:"Sequential",0x27:"Silent sequential",
             0x28:"High speed sequential 1",0x29:"High speed sequential 2",
             0x48:"Pro Cap SH1",0x49:"Pro Cap SH2",
             0x04:"Self-timer 12s",0x05:"Self-timer 2s",
             0x24:"Silent self-timer 2s",0x06:"Custom self-timer"},
    0xD004: {0x8001:"Digital ESP",0x0002:"Center weighted",
             0x0004:"Spot",0x8011:"Spot highlight",0x8012:"Spot shadow"},
    0xD1D0: {1:"Human",2:"Motorsports",3:"Airplanes",
             4:"Trains",5:"Birds",6:"Dogs and cats",7:"Off"},
    0xD1B9: {1:"Off",2:"On tripod",3:"On handheld"},
    0xD003: {1:"S-AF",0x8002:"MF"},
    0xD005: {2:"On/Fill"},
    0xD0C7: {0x0107:"Large Fine",0x0106:"Large Normal"},
}

def decode_value(prop_code, raw_bytes):
    if not raw_bytes: return None
    if prop_code == 0xD01C:
        if len(raw_bytes) >= 2:
            denom = struct.unpack_from('<H', raw_bytes, 0)[0]
            return f"1/{denom}" if denom > 1 else f"{denom}\""
    if prop_code in (0xD008, 0xD00F):
        if len(raw_bytes) >= 2:
            val = struct.unpack_from('<h', raw_bytes, 0)[0]
            return f"{val/1000:+.1f} EV"
    if prop_code == 0xD002:
        if len(raw_bytes) >= 2:
            val = struct.unpack_from('<H', raw_bytes, 0)[0]
            return f"f/{val/10:.1f}"
    if prop_code == 0xD1C0:
        if len(raw_bytes) >= 4:
            val = struct.unpack_from('<I', raw_bytes, 0)[0]
            return "Auto" if val in (0, 0xFFFFFFFF) else str(val)
    if prop_code in (0xD033, 0xD034):
        if len(raw_bytes) >= 2:
            val = struct.unpack_from('<h', raw_bytes, 0)[0]
            axis = 'A' if prop_code == 0xD033 else 'G'
            return f"{axis}{val:+d}"
    if prop_code in PROP_VALUES and len(raw_bytes) >= 2:
        val = struct.unpack_from('<H', raw_bytes, 0)[0]
        return PROP_VALUES[prop_code].get(val, str(val))
    if len(raw_bytes) == 2: return str(struct.unpack_from('<H', raw_bytes, 0)[0])
    if len(raw_bytes) == 4: return str(struct.unpack_from('<I', raw_bytes, 0)[0])
    return raw_bytes.hex()

def encode_value(prop_code, value_str):
    if prop_code == 0xD01C:
        if value_str.startswith("1/"):
            return struct.pack('<I', (1 << 16) | int(value_str[2:]))
        return None
    if prop_code in (0xD008, 0xD00F):
        s = value_str.replace(' EV','').replace('+','').strip()
        return struct.pack('<h', int(float(s)*1000))
    if prop_code == 0xD002:
        return struct.pack('<H', int(float(value_str.replace('f/','').strip())*10))
    if prop_code == 0xD1C0:
        return struct.pack('<I', 0 if value_str=="Auto" else int(value_str))
    if prop_code in (0xD033, 0xD034):
        return struct.pack('<h', int(value_str[1:]))
    if prop_code in PROP_VALUES:
        rev = {v: k for k, v in PROP_VALUES[prop_code].items()}
        if value_str in rev: return struct.pack('<H', rev[value_str])
        try: return struct.pack('<H', int(value_str))
        except ValueError: return None
    try:
        val = int(value_str)
        return struct.pack('<H', val) if val < 65536 else struct.pack('<I', val)
    except ValueError: return None

# ── DEVICE DISCOVERY ──────────────────────────────────────────────────────────

def find_olympus_cameras():
    result = subprocess.run(
        ['powershell', '-NonInteractive', '-command',
         'Get-PnpDevice | Where-Object {$_.Class -eq "WPD"} | '
         'Select-Object FriendlyName, InstanceId | ConvertTo-Json'],
        capture_output=True, text=True)
    cameras = []
    if result.returncode == 0:
        try:
            devices = json.loads(result.stdout)
            if isinstance(devices, dict): devices = [devices]
            for d in devices:
                name = d.get('FriendlyName','')
                iid  = d.get('InstanceId','')
                if ('VID_33A2' in iid or 'Olympus' in name or
                    'OM-1' in name or 'OM SYSTEM' in name or
                    'E-M' in name or 'OM-5' in name):
                    cameras.append((iid, name))
        except Exception: pass
    return cameras

# ── C# WPD BRIDGE ────────────────────────────────────────────────────────────

_CSHARP_SOURCE = r"""
using System;
using System.Runtime.InteropServices;
using System.Text;

[ComImport, Guid("625E2DF8-6392-4CF0-9AD1-3CFA5F17775C"),
 InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IPortableDevice {
    void Open([MarshalAs(UnmanagedType.LPWStr)] string pszPnPDeviceID,
              [MarshalAs(UnmanagedType.IUnknown)] object pClientInfo);
    void SendCommand(uint dwFlags,
                     [MarshalAs(UnmanagedType.IUnknown)] object pParameters,
                     [MarshalAs(UnmanagedType.IUnknown)] out object ppResults);
    void Content([MarshalAs(UnmanagedType.IUnknown)] out object ppContent);
    void Capabilities([MarshalAs(UnmanagedType.IUnknown)] out object ppCapabilities);
    void Cancel();
    void Close();
    void Advise(uint f, [MarshalAs(UnmanagedType.IUnknown)] object cb,
                [MarshalAs(UnmanagedType.IUnknown)] object p,
                [MarshalAs(UnmanagedType.LPWStr)] out string cookie);
    void Unadvise([MarshalAs(UnmanagedType.LPWStr)] string cookie);
    void GetPnPDeviceID([MarshalAs(UnmanagedType.LPWStr)] out string id);
}

[ComImport, Guid("6A96ED84-7C73-4480-9938-BF5AF477D426"),
 InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IPortableDeviceContent {
    void EnumObjects(uint f, [MarshalAs(UnmanagedType.LPWStr)] string parent,
                     [MarshalAs(UnmanagedType.IUnknown)] object pFilter,
                     [MarshalAs(UnmanagedType.IUnknown)] out object ppEnum);
    void Properties([MarshalAs(UnmanagedType.IUnknown)] out object ppProperties);
    void Transfer([MarshalAs(UnmanagedType.IUnknown)] out object ppResources);
    void CreateObjectWithPropertiesOnly([MarshalAs(UnmanagedType.IUnknown)] object pValues,
                                        [MarshalAs(UnmanagedType.LPWStr)] out string ppszObjectID);
    void CreateObjectWithPropertiesAndData([MarshalAs(UnmanagedType.IUnknown)] object pValues,
                                           [MarshalAs(UnmanagedType.IUnknown)] out object ppData,
                                           out uint pdwOptimalWriteBufferSize,
                                           [MarshalAs(UnmanagedType.LPWStr)] out string ppszCookie);
    void Delete(uint f, [MarshalAs(UnmanagedType.IUnknown)] object pObjectIDs,
                [MarshalAs(UnmanagedType.IUnknown)] out object ppResults);
    void GetObjectIDsFromPersistentUniqueIDs(
                [MarshalAs(UnmanagedType.IUnknown)] object pPersistentUniqueIDs,
                [MarshalAs(UnmanagedType.IUnknown)] out object ppObjectIDs);
    void Cancel();
    void Move([MarshalAs(UnmanagedType.IUnknown)] object pObjectIDs,
              [MarshalAs(UnmanagedType.LPWStr)] string pszDestinationFolderObjectID,
              [MarshalAs(UnmanagedType.IUnknown)] out object ppResults);
    void Copy([MarshalAs(UnmanagedType.IUnknown)] object pObjectIDs,
              [MarshalAs(UnmanagedType.LPWStr)] string pszDestinationFolderObjectID,
              [MarshalAs(UnmanagedType.IUnknown)] out object ppResults);
}

[ComImport, Guid("7F6D695C-03DF-4439-A809-59266BEEE3A6"),
 InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IPortableDeviceProperties {
    void GetSupportedProperties([MarshalAs(UnmanagedType.LPWStr)] string pszObjectID,
                                [MarshalAs(UnmanagedType.IUnknown)] out object ppKeys);
    void GetPropertyAttributes([MarshalAs(UnmanagedType.LPWStr)] string pszObjectID,
                               IntPtr key,
                               [MarshalAs(UnmanagedType.IUnknown)] out object ppAttributes);
    void GetValues([MarshalAs(UnmanagedType.LPWStr)] string pszObjectID,
                   [MarshalAs(UnmanagedType.IUnknown)] object pKeys,
                   [MarshalAs(UnmanagedType.IUnknown)] out object ppValues);
    void SetValues([MarshalAs(UnmanagedType.LPWStr)] string pszObjectID,
                   [MarshalAs(UnmanagedType.IUnknown)] object pValues,
                   [MarshalAs(UnmanagedType.IUnknown)] out object ppResults);
    void Delete([MarshalAs(UnmanagedType.LPWStr)] string pszObjectID,
                [MarshalAs(UnmanagedType.IUnknown)] object pKeys);
    void Cancel();
}

[ComImport, Guid("6848F6F2-3155-4F86-B6F5-263EEEAB3143"),
 InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IPortableDeviceValues {
    void GetCount(out uint pcelt);
    void GetAt(uint index, IntPtr pKey, IntPtr pValue);
    void SetValue(IntPtr key, IntPtr pValue);
    void GetValue(IntPtr key, IntPtr pValue);
    void SetStringValue(IntPtr key, [MarshalAs(UnmanagedType.LPWStr)] string Value);
    void GetStringValue(IntPtr key, [MarshalAs(UnmanagedType.LPWStr)] out string pValue);
    void SetUnsignedIntegerValue(IntPtr key, uint Value);
    void GetUnsignedIntegerValue(IntPtr key, out uint pValue);
    void SetSignedIntegerValue(IntPtr key, int Value);
    void GetSignedIntegerValue(IntPtr key, out int pValue);
    void SetUnsignedLargeIntegerValue(IntPtr key, ulong Value);
    void GetUnsignedLargeIntegerValue(IntPtr key, out ulong pValue);
    void SetSignedLargeIntegerValue(IntPtr key, long Value);
    void GetSignedLargeIntegerValue(IntPtr key, out long pValue);
    void SetFloatValue(IntPtr key, float Value);
    void GetFloatValue(IntPtr key, out float pValue);
    void SetErrorValue(IntPtr key, int Value);
    void GetErrorValue(IntPtr key, out int pValue);
    void SetKeyValue(IntPtr key, IntPtr Value);
    void GetKeyValue(IntPtr key, IntPtr pValue);
    void SetBoolValue(IntPtr key, int Value);
    void GetBoolValue(IntPtr key, out int pValue);
    void SetIUnknownValue(IntPtr key, [MarshalAs(UnmanagedType.IUnknown)] object pValue);
    void GetIUnknownValue(IntPtr key, [MarshalAs(UnmanagedType.IUnknown)] out object ppValue);
    void SetGuidValue(IntPtr key, ref Guid Value);
    void GetGuidValue(IntPtr key, out Guid pValue);
    void SetBufferValue(IntPtr key, byte[] pValue, uint cbValue);
    void GetBufferValue(IntPtr key, out IntPtr ppValue, out uint pcbValue);
    void SetIPortableDeviceValuesValue(IntPtr key, [MarshalAs(UnmanagedType.IUnknown)] object pValue);
    void GetIPortableDeviceValuesValue(IntPtr key, [MarshalAs(UnmanagedType.IUnknown)] out object ppValue);
    void SetIPortableDeviceKeyCollectionValue(IntPtr key, [MarshalAs(UnmanagedType.IUnknown)] object pValue);
    void GetIPortableDeviceKeyCollectionValue(IntPtr key, [MarshalAs(UnmanagedType.IUnknown)] out object ppValue);
    void SetIPortableDevicePropVariantCollectionValue(IntPtr key, [MarshalAs(UnmanagedType.IUnknown)] object pValue);
    void GetIPortableDevicePropVariantCollectionValue(IntPtr key, [MarshalAs(UnmanagedType.IUnknown)] out object ppValue);
    void SetIPortableDeviceValuesCollectionValue(IntPtr key, [MarshalAs(UnmanagedType.IUnknown)] object pValue);
    void GetIPortableDeviceValuesCollectionValue(IntPtr key, [MarshalAs(UnmanagedType.IUnknown)] out object ppValue);
    void RemoveValue(IntPtr key);
    void CopyValuesFromPropertyStore([MarshalAs(UnmanagedType.IUnknown)] object pStore);
    void CopyValuesToPropertyStore([MarshalAs(UnmanagedType.IUnknown)] object pStore);
    void Clear();
}

[ComImport, Guid("DADA2357-E0AD-492E-98DB-DD61C53BA353"),
 InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IPortableDeviceKeyCollection {
    void GetCount(out uint pcelt);
    void GetAt(uint dwIndex, IntPtr pKey);
    void Add(IntPtr pKey);
    void Clear();
    void RemoveAt(uint dwIndex);
}

[ComImport, Guid("A1567595-4C2F-4574-A6FA-ECEF917B9A40"),
 InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IPortableDeviceManager {
    void GetDevices(IntPtr pPnPDeviceIDs, ref uint pcPnPDeviceIDs);
    void RefreshDeviceList();
    void GetDeviceFriendlyName([MarshalAs(UnmanagedType.LPWStr)] string id,
                               IntPtr pName, ref uint pcch);
    void GetDeviceDescription([MarshalAs(UnmanagedType.LPWStr)] string id,
                              IntPtr pDesc, ref uint pcch);
    void GetDeviceManufacturer([MarshalAs(UnmanagedType.LPWStr)] string id,
                               IntPtr pMfr, ref uint pcch);
}

[UnmanagedFunctionPointer(CallingConvention.StdCall)]
delegate int OpenFn(IntPtr pThis, [MarshalAs(UnmanagedType.LPWStr)] string id, IntPtr pClientInfo);
[UnmanagedFunctionPointer(CallingConvention.StdCall)]
delegate int CloseFn(IntPtr pThis);

class Program {
    [DllImport("ole32.dll")] static extern int CoInitializeEx(IntPtr p, uint m);
    [DllImport("ole32.dll")] static extern void CoUninitialize();
    [DllImport("ole32.dll")]
    static extern int CoCreateInstance(ref Guid c, IntPtr o, uint ctx,
        ref Guid i, out IntPtr ppv);

    static IntPtr CreateRaw(string cs, string is_) {
        Guid c=new Guid(cs); Guid i=new Guid(is_); IntPtr p;
        int hr=CoCreateInstance(ref c,IntPtr.Zero,1,ref i,out p);
        if(hr!=0) throw new COMException("hr=0x"+hr.ToString("X8"),hr);
        return p;
    }
    static object CreateObj(string cs, string is_) {
        IntPtr p=CreateRaw(cs,is_); object o=Marshal.GetObjectForIUnknown(p);
        Marshal.Release(p); return o;
    }
    static IntPtr MK(string g, uint p) {
        byte[] b=new byte[20]; Array.Copy(new Guid(g).ToByteArray(),b,16);
        b[16]=(byte)p; b[17]=(byte)(p>>8); b[18]=(byte)(p>>16); b[19]=(byte)(p>>24);
        IntPtr ptr=Marshal.AllocHGlobal(20); Marshal.Copy(b,0,ptr,20); return ptr;
    }
    static string Esc(string s) {
        if(s==null)return"";
        return s.Replace("\\","\\\\").Replace("\"","'").Replace("\r","").Replace("\n"," ");
    }

    static IPortableDeviceValues MakeClientInfo() {
        object v=CreateObj("0C15D503-D017-47CE-9016-7B3F978721CC",
                           "6848F6F2-3155-4F86-B6F5-263EEEAB3143");
        var vals=(IPortableDeviceValues)v;
        string CIG="204D9F0C-2292-4080-9F42-40664E70F859";
        IntPtr k2=MK(CIG,2),k3=MK(CIG,3),k4=MK(CIG,4),k5=MK(CIG,5);
        vals.SetStringValue(k2,"OlyProbe");
        vals.SetUnsignedIntegerValue(k3,1);
        vals.SetUnsignedIntegerValue(k4,0);
        vals.SetUnsignedIntegerValue(k5,0);
        Marshal.FreeHGlobal(k2);Marshal.FreeHGlobal(k3);
        Marshal.FreeHGlobal(k4);Marshal.FreeHGlobal(k5);
        return vals;
    }

    static string GetWpdId() {
        var mgr=(IPortableDeviceManager)CreateObj(
            "0AF10CEC-2ECD-4B92-9581-34F6AE0637F3",
            "A1567595-4C2F-4574-A6FA-ECEF917B9A40");
        uint count=0; mgr.GetDevices(IntPtr.Zero,ref count);
        if(count==0)return null;
        IntPtr pArr=Marshal.AllocCoTaskMem((int)(count*IntPtr.Size));
        try {
            for(int i=0;i<(int)count;i++) Marshal.WriteIntPtr(pArr,i*IntPtr.Size,IntPtr.Zero);
            mgr.GetDevices(pArr,ref count);
            IntPtr pStr=Marshal.ReadIntPtr(pArr,0);
            string id=Marshal.PtrToStringUni(pStr);
            if(pStr!=IntPtr.Zero)Marshal.FreeCoTaskMem(pStr);
            return id;
        } finally { Marshal.FreeCoTaskMem(pArr); }
    }

    static IntPtr OpenDeviceRaw(string wpdId, IPortableDeviceValues ci) {
        IntPtr pDev=CreateRaw("F7C0039A-4762-488A-B4B3-760EF9A1BA9B",
                              "625E2DF8-6392-4CF0-9AD1-3CFA5F17775C");
        IntPtr pVals=Marshal.GetIUnknownForObject(ci);
        IntPtr vt=Marshal.ReadIntPtr(pDev);
        IntPtr fnPtr=Marshal.ReadIntPtr(vt,3*IntPtr.Size);
        var fn=(OpenFn)Marshal.GetDelegateForFunctionPointer(fnPtr,typeof(OpenFn));
        int hr=fn(pDev,wpdId,pVals); Marshal.Release(pVals);
        if(hr!=0){Marshal.Release(pDev);throw new COMException("Open hr=0x"+hr.ToString("X8"),hr);}
        return pDev;
    }

    static void CloseDeviceRaw(IntPtr pDev) {
        IntPtr vt=Marshal.ReadIntPtr(pDev);
        IntPtr fnPtr=Marshal.ReadIntPtr(vt,8*IntPtr.Size);
        var fn=(CloseFn)Marshal.GetDelegateForFunctionPointer(fnPtr,typeof(CloseFn));
        fn(pDev); Marshal.Release(pDev);
    }

    static int Main(string[] args) {
        CoInitializeEx(IntPtr.Zero,0);
        string cmd=args.Length>0?args[0]:"list";
        uint propCode=args.Length>1?Convert.ToUInt32(args[1],16):0;
        string valueHex=args.Length>2?args[2]:"";

        try {
            string wpdId=GetWpdId();
            if(wpdId==null){Console.WriteLine("{\"ok\":false,\"error\":\"No WPD device\"}");return 0;}
            Console.Error.WriteLine("wpdId="+wpdId);

            if(cmd=="list") {
                Console.WriteLine("{\"ok\":true,\"device\":\""+Esc(wpdId)+"\"}");

            } else if(cmd=="props") {
                // Get all supported properties on the DEVICE object
                var ci=MakeClientInfo();
                IntPtr pDev=OpenDeviceRaw(wpdId,ci);
                object devObj=Marshal.GetObjectForIUnknown(pDev);
                var dev=(IPortableDevice)devObj;

                object contentObj; dev.Content(out contentObj);
                var content=(IPortableDeviceContent)contentObj;
                Console.Error.WriteLine("content ok");

                object propsObj; content.Properties(out propsObj);
                var props=(IPortableDeviceProperties)propsObj;
                Console.Error.WriteLine("props ok");

                // Get supported properties for DEVICE object
                object keysObj; props.GetSupportedProperties("DEVICE", out keysObj);
                var keys=(IPortableDeviceKeyCollection)keysObj;
                uint keyCount; keys.GetCount(out keyCount);
                Console.Error.WriteLine("supported props count="+keyCount);

                var sb=new StringBuilder("{\"ok\":true,\"props\":[");
                for(uint ki=0;ki<keyCount;ki++){
                    IntPtr kPtr=Marshal.AllocHGlobal(20);
                    for(int bi=0;bi<20;bi++) Marshal.WriteByte(kPtr,bi,0);
                    try {
                        keys.GetAt(ki,kPtr);
                        byte[] kb=new byte[20]; Marshal.Copy(kPtr,kb,0,20);
                        Guid g=new Guid(new byte[]{kb[0],kb[1],kb[2],kb[3],kb[4],kb[5],kb[6],kb[7],
                                                    kb[8],kb[9],kb[10],kb[11],kb[12],kb[13],kb[14],kb[15]});
                        uint pid=BitConverter.ToUInt32(kb,16);
                        if(ki>0) sb.Append(",");
                        sb.Append("{\"guid\":\""+g+"\",\"pid\":"+pid+"}");
                    } catch{}
                    Marshal.FreeHGlobal(kPtr);
                }
                sb.Append("]}");
                CloseDeviceRaw(pDev);
                Console.WriteLine(sb.ToString());

            } else if(cmd=="get") {
                // Read a property via IPortableDeviceProperties::GetValues
                var ci=MakeClientInfo();
                IntPtr pDev=OpenDeviceRaw(wpdId,ci);
                object devObj=Marshal.GetObjectForIUnknown(pDev);
                var dev=(IPortableDevice)devObj;

                object contentObj; dev.Content(out contentObj);
                var content=(IPortableDeviceContent)contentObj;
                object propsObj; content.Properties(out propsObj);
                var props=(IPortableDeviceProperties)propsObj;

                // Build key collection with our property
                // Try Olympus GUID first: need to figure out what GUID they use in WPD
                // The MTP prop code maps to a WPD PROPERTYKEY
                // Standard MTP props use {F29F85E0-4FF9-1068-AB91-08002B27B3D9} as GUID? No.
                // Actually Olympus vendor props: likely use the device object directly
                // Let's try getting ALL values and see what comes back
                object allValues; props.GetValues("DEVICE", null, out allValues);
                var vals=(IPortableDeviceValues)allValues;
                uint vcount; vals.GetCount(out vcount);
                Console.Error.WriteLine("DEVICE property count="+vcount);

                var sb=new StringBuilder("{\"ok\":true,\"count\":"+vcount+",\"props\":[");
                for(uint vi=0;vi<vcount;vi++){
                    IntPtr kPtr=Marshal.AllocHGlobal(20);
                    IntPtr pvPtr=Marshal.AllocHGlobal(16);
                    for(int bi=0;bi<20;bi++) Marshal.WriteByte(kPtr,bi,0);
                    for(int bi=0;bi<16;bi++) Marshal.WriteByte(pvPtr,bi,0);
                    try {
                        vals.GetAt(vi,kPtr,pvPtr);
                        byte[] kb=new byte[20]; Marshal.Copy(kPtr,kb,0,20);
                        byte[] pv=new byte[16]; Marshal.Copy(pvPtr,pv,0,16);
                        Guid g=new Guid(new byte[]{kb[0],kb[1],kb[2],kb[3],kb[4],kb[5],kb[6],kb[7],
                                                    kb[8],kb[9],kb[10],kb[11],kb[12],kb[13],kb[14],kb[15]});
                        uint pid=BitConverter.ToUInt32(kb,16);
                        ushort vt=BitConverter.ToUInt16(pv,0);
                        string valHex=BitConverter.ToString(pv,8,8).Replace("-","");
                        if(vi>0) sb.Append(",");
                        sb.Append("{\"guid\":\""+g+"\",\"pid\":"+pid+",\"vt\":"+vt+",\"val\":\""+valHex+"\"}");
                    } catch{}
                    Marshal.FreeHGlobal(kPtr);
                    Marshal.FreeHGlobal(pvPtr);
                }
                sb.Append("]}");
                CloseDeviceRaw(pDev);
                Console.WriteLine(sb.ToString());

            } else if(cmd=="getprop") {
                // Read a specific property using key collection
                string OLY = "4D545058-8900-40B3-8F1D-DC246E1E8370";
                var ci=MakeClientInfo();
                IntPtr pDev=OpenDeviceRaw(wpdId,ci);
                object devObj=Marshal.GetObjectForIUnknown(pDev);
                var dev=(IPortableDevice)devObj;
                object contentObj; dev.Content(out contentObj);
                var content=(IPortableDeviceContent)contentObj;
                object propsObj; content.Properties(out propsObj);
                var props=(IPortableDeviceProperties)propsObj;

                // Build key collection with our property key
                object keysObj=CreateObj("DE2D022D-2480-43BE-97F0-D1FA2CF98F4F",
                                          "DADA2357-E0AD-492E-98DB-DD61C53BA353");
                var keyColl=(IPortableDeviceKeyCollection)keysObj;
                IntPtr kPtr=MK(OLY,(uint)propCode);
                keyColl.Add(kPtr);
                Marshal.FreeHGlobal(kPtr);

                object valuesObj; props.GetValues("DEVICE",keyColl,out valuesObj);
                var values=(IPortableDeviceValues)valuesObj;

                // Read the value
                IntPtr kPtr2=MK(OLY,(uint)propCode);
                byte[] result=new byte[8];
                try {
                    // Try as uint first (vt=18/19)
                    uint uval=0;
                    values.GetUnsignedIntegerValue(kPtr2,out uval);
                    result=BitConverter.GetBytes(uval);
                    Console.Error.WriteLine("uint value="+uval+" (0x"+uval.ToString("X")+")");
                } catch {
                    try {
                        ulong ulval=0;
                        values.GetUnsignedLargeIntegerValue(kPtr2,out ulval);
                        result=BitConverter.GetBytes(ulval);
                        Console.Error.WriteLine("ulong value="+ulval);
                    } catch(Exception exV) {
                        Console.Error.WriteLine("GetValue failed: "+exV.Message);
                    }
                }
                Marshal.FreeHGlobal(kPtr2);
                CloseDeviceRaw(pDev);
                Console.WriteLine("{\"ok\":true,\"value\":\""+Convert.ToBase64String(result)+"\"}");

            } else if(cmd=="setprop") {
                // Set a property value
                string OLY = "4D545058-8900-40B3-8F1D-DC246E1E8370";
                byte[] setBytes=new byte[valueHex.Length/2];
                for(int i=0;i<setBytes.Length;i++)
                    setBytes[i]=Convert.ToByte(valueHex.Substring(i*2,2),16);
                uint setVal=setBytes.Length>=4?BitConverter.ToUInt32(setBytes,0):
                            setBytes.Length>=2?BitConverter.ToUInt16(setBytes,0):(uint)setBytes[0];

                var ci=MakeClientInfo();
                IntPtr pDev=OpenDeviceRaw(wpdId,ci);
                object devObj=Marshal.GetObjectForIUnknown(pDev);
                var dev=(IPortableDevice)devObj;
                object contentObj; dev.Content(out contentObj);
                var content=(IPortableDeviceContent)contentObj;
                object propsObj; content.Properties(out propsObj);
                var props=(IPortableDeviceProperties)propsObj;

                object setValsObj=CreateObj("0C15D503-D017-47CE-9016-7B3F978721CC",
                                            "6848F6F2-3155-4F86-B6F5-263EEEAB3143");
                var setVals=(IPortableDeviceValues)setValsObj;
                IntPtr kPtr=MK(OLY,(uint)propCode);
                setVals.SetUnsignedIntegerValue(kPtr,setVal);
                Marshal.FreeHGlobal(kPtr);

                object results; props.SetValues("DEVICE",setVals,out results);
                Console.Error.WriteLine("SetValues ok");
                CloseDeviceRaw(pDev);
                Console.WriteLine("{\"ok\":true}");

            } else if(cmd=="set") {
                Console.WriteLine("{\"ok\":false,\"error\":\"set not yet implemented\"}");
            } else {
                Console.WriteLine("{\"ok\":false,\"error\":\"Unknown cmd: "+cmd+"\"}");
            }
        } catch(Exception ex) {
            Console.WriteLine("{\"ok\":false,\"error\":\""+Esc(ex.Message)+
                "\",\"type\":\""+Esc(ex.GetType().Name)+"\"}");
        } finally { CoUninitialize(); }
        return 0;
    }
}
"""

_CSC_PATHS = [
    r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe",
    r"C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe",
]

_bridge_exe = None

def _get_bridge_exe():
    global _bridge_exe
    if _bridge_exe and os.path.exists(_bridge_exe):
        return _bridge_exe
    csc = next((p for p in _CSC_PATHS if os.path.exists(p)), None)
    if not csc:
        raise RuntimeError("csc.exe not found")
    tmpdir   = tempfile.mkdtemp(prefix="olyprobe_")
    cs_path  = os.path.join(tmpdir, "wpd_bridge.cs")
    exe_path = os.path.join(tmpdir, "wpd_bridge.exe")
    with open(cs_path, 'w', encoding='utf-8') as f:
        f.write(_CSHARP_SOURCE)
    result = subprocess.run([csc, "/nologo", f"/out:{exe_path}", cs_path],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"C# compile error:\n{result.stdout}\n{result.stderr}")
    _bridge_exe = exe_path
    return exe_path


def _bridge(args, timeout=20):
    exe = _get_bridge_exe()
    result = subprocess.run([exe]+[str(a) for a in args],
                            capture_output=True, text=True, timeout=timeout)
    stderr = result.stderr.strip()
    if stderr: print(f"  [bridge]: {stderr}")
    output = result.stdout.strip()
    if not output:
        return {"ok": False, "error": result.stderr.strip() or "No output"}
    try: return json.loads(output)
    except Exception: return {"ok": False, "error": output}


class USBCamera:
    def __init__(self):
        self._device_id = None
        self.model = None
        self._connected = False

    def connect(self, device_id=None):
        if not is_usb_permitted():
            raise PermissionError("USB tethering requires a premium subscription")
        result = _bridge(["list"])
        if not result.get("ok"):
            raise ConnectionError("No camera found. Connect via USB in Raw/Control mode.")
        self._device_id = result.get("device")
        self._connected = True
        return True

    def disconnect(self):
        self._connected = False

    @property
    def connected(self):
        return self._connected


if __name__ == "__main__":
    import datetime

    print("OlyProbe USB Camera Module")
    print("=" * 40)

    print("Compiling C# bridge...")
    try:
        exe = _get_bridge_exe()
        mtime = datetime.datetime.fromtimestamp(os.path.getmtime(exe))
        print(f"Compiled OK  [{mtime.strftime('%H:%M:%S')}]")
    except Exception as e:
        print(f"Compile error: {e}")
        exit()

    print()
    print("Step 1: Get WPD device...")
    r = _bridge(["list"])
    print(f"  {r}")
    if not r.get("ok"): exit()

    print()
    print("Step 2: Read specific properties using Olympus GUID...")
    # GUID 4d545058-8900-40b3-8f1d-dc246e1e8370 contains all Olympus MTP props
    # PIDs are decimal MTP property codes (0xD002=53250, 0xD009=53257 etc.)
    r2 = _bridge(["get"])
    if r2.get("ok"):
        oly_guid = "4d545058-8900-40b3-8f1d-dc246e1e8370"
        props = {p['pid']: p for p in r2.get('props', [])
                 if p['guid'] == oly_guid}
        print(f"  Olympus properties found: {len(props)}")
        print()
        # Decode known properties
        import struct, base64
        for prop_code, info in sorted(PROP_MAP.items()):
            pid = prop_code  # pid == MTP code in decimal
            if pid in props:
                raw_hex = props[pid]['val']
                raw = bytes.fromhex(raw_hex)
                decoded = decode_value(prop_code, raw)
                print(f"  0x{prop_code:04X} {info['name']:30s} raw={raw_hex[:8]}  decoded={decoded}")
    else:
        print(f"  {r2}")

    print()
    print("Step 3: Read ISO (0xD1C0) via targeted getprop...")
    r3 = _bridge(["getprop", f"{0xD1C0:04X}"])
    print(f"  {r3}")
    if r3.get("ok") and r3.get("value"):
        import base64
        raw = base64.b64decode(r3["value"])
        print(f"  Decoded: {decode_value(0xD1C0, raw)}")

    print()
    print("Step 4: Read Drive Mode (0xD009) via targeted getprop...")
    r4 = _bridge(["getprop", f"{0xD009:04X}"])
    print(f"  {r4}")
    if r4.get("ok") and r4.get("value"):
        import base64
        raw = base64.b64decode(r4["value"])
        print(f"  Decoded: {decode_value(0xD009, raw)}")

    print()
    print("Step 5: Set ISO to 400 (0xD1C0 = 0x00000190)...")
    r5 = _bridge(["setprop", f"{0xD1C0:04X}", "90010000"])
    print(f"  {r5}")

    print()
    print("Step 6: Read ISO back to verify...")
    r6 = _bridge(["getprop", f"{0xD1C0:04X}"])
    print(f"  {r6}")
    if r6.get("ok") and r6.get("value"):
        import base64
        raw = base64.b64decode(r6["value"])
        print(f"  ISO: {decode_value(0xD1C0, raw)}")

    print()
    print("Step 7: Restore ISO to Auto (0xFFFFFFFF)...")
    r7 = _bridge(["setprop", f"{0xD1C0:04X}", "ffffffff"])
    print(f"  {r7}")
