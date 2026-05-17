"""
usb_camera.py — Olympus/OM SYSTEM USB tethering via Windows WPD/MTP

Two read paths:
1. WPD IPortableDeviceProperties::GetValues — reads 295 Olympus properties (works for
   electronic settings like ISO, WB, drive, AF, flash etc.)
2. DeviceIoControl IOCTL_MTP_CUSTOM_COMMAND — sends raw MTP vendor commands like 0x9486
   to read exposure triangle values (shutter, aperture, expcomp) which WPD caches as 0

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
import base64

def _purge_old_bridges():
    pattern = os.path.join(tempfile.gettempdir(), "olyprobe_*")
    for path in glob.glob(pattern):
        try:
            shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass

_purge_old_bridges()

def is_usb_permitted():
    return True

# ── PROPERTY MAP ──────────────────────────────────────────────────────────────

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

def decode_value(prop_code, raw_bytes):
    if not raw_bytes: return None
    if prop_code == 0xD01C:
        if len(raw_bytes) >= 4:
            denom = struct.unpack_from('<H', raw_bytes, 0)[0]
            numer = struct.unpack_from('<H', raw_bytes, 2)[0]
            if denom == 0: return "Bulb"
            if numer <= 1: return f"1/{denom}"
            secs = numer / denom
            return f"{int(secs)}\"" if secs == int(secs) else f"{secs:.1f}\""
    if prop_code in (0xD008, 0xD00F):
        if len(raw_bytes) >= 2:
            val = struct.unpack_from('<h', raw_bytes, 0)[0]
            if abs(val) <= 5: return "0.0"
            return f"{val/1000:+.1f}"
    if prop_code == 0xD002:
        if len(raw_bytes) >= 2:
            val = struct.unpack_from('<H', raw_bytes, 0)[0]
            return f"f/{val/10:.1f}" if val > 0 else "f/--"
    if prop_code == 0xD1C0:
        if len(raw_bytes) >= 4:
            val = struct.unpack_from('<I', raw_bytes, 0)[0]
            return "Auto" if val in (0, 0xFFFFFFFF) else str(val)
    if len(raw_bytes) == 2: return str(struct.unpack_from('<H', raw_bytes, 0)[0])
    if len(raw_bytes) == 4: return str(struct.unpack_from('<I', raw_bytes, 0)[0])
    return raw_bytes.hex()


def encode_value(prop_code, value_str):
    if prop_code == 0xD01C:
        if '/' in value_str:
            parts = value_str.split('/')
            return struct.pack('<HH', int(parts[1]), int(parts[0]))
        return None
    if prop_code in (0xD008, 0xD00F):
        s = value_str.replace('+','').replace(' EV','').strip()
        return struct.pack('<h', int(float(s)*1000))
    if prop_code == 0xD002:
        return struct.pack('<H', int(float(value_str.replace('f/','').strip())*10))
    if prop_code == 0xD1C0:
        val = 0xFFFFFFFF if value_str == "Auto" else int(value_str)
        return struct.pack('<I', val)
    try:
        val = int(value_str)
        return struct.pack('<H', val) if val < 65536 else struct.pack('<I', val)
    except ValueError:
        return None

# ── C# BRIDGE ────────────────────────────────────────────────────────────────

_CSHARP_SOURCE = r"""
using System;
using System.Runtime.InteropServices;
using System.Text;

// ── WPD interfaces ────────────────────────────────────────────────────────────

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

// ── SetupDi structs (top-level, required for C# 5) ──────────────────────────

[StructLayout(LayoutKind.Sequential)]
struct SP_DEVINFO_DATA {
    public uint cbSize;
    public Guid ClassGuid;
    public uint DevInst;
    public IntPtr Reserved;
}

[StructLayout(LayoutKind.Sequential, CharSet=CharSet.Auto)]
struct SP_DEVICE_INTERFACE_DATA {
    public uint cbSize;
    public Guid InterfaceClassGuid;
    public uint Flags;
    public IntPtr Reserved;
}

[StructLayout(LayoutKind.Sequential, CharSet=CharSet.Auto)]
struct SP_DEVICE_INTERFACE_DETAIL_DATA {
    public uint cbSize;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst=512)]
    public string DevicePath;
}

// ── Delegates ────────────────────────────────────────────────────────────────

[UnmanagedFunctionPointer(CallingConvention.StdCall)]
delegate int OpenFn(IntPtr pThis, [MarshalAs(UnmanagedType.LPWStr)] string id, IntPtr pClientInfo);
[UnmanagedFunctionPointer(CallingConvention.StdCall)]
delegate int CloseFn(IntPtr pThis);
[UnmanagedFunctionPointer(CallingConvention.StdCall)]
delegate int AddPVDelegate(IntPtr pThis, IntPtr pv);
[UnmanagedFunctionPointer(CallingConvention.StdCall)]
delegate int GetCollCountFn(IntPtr pThis, out uint count);
[UnmanagedFunctionPointer(CallingConvention.StdCall)]
delegate int GetCollAtFn(IntPtr pThis, uint index, IntPtr pv);

// ── Main Program ─────────────────────────────────────────────────────────────

class Program {
    [DllImport("ole32.dll")] static extern int CoInitializeEx(IntPtr p, uint m);
    [DllImport("ole32.dll")] static extern void CoUninitialize();
    [DllImport("ole32.dll")]
    static extern int CoCreateInstance(ref Guid c, IntPtr o, uint ctx,
        ref Guid i, out IntPtr ppv);

    // SetupDi
    [DllImport("setupapi.dll", CharSet=CharSet.Auto, SetLastError=true)]
    static extern IntPtr SetupDiGetClassDevs(ref Guid ClassGuid, IntPtr Enumerator,
        IntPtr hwndParent, uint Flags);
    [DllImport("setupapi.dll", CharSet=CharSet.Auto, SetLastError=true)]
    static extern bool SetupDiEnumDeviceInterfaces(IntPtr DeviceInfoSet,
        IntPtr DeviceInfoData, ref Guid InterfaceClassGuid, uint MemberIndex,
        ref SP_DEVICE_INTERFACE_DATA DeviceInterfaceData);
    [DllImport("setupapi.dll", CharSet=CharSet.Auto, SetLastError=true)]
    static extern bool SetupDiGetDeviceInterfaceDetail(IntPtr DeviceInfoSet,
        ref SP_DEVICE_INTERFACE_DATA DeviceInterfaceData,
        ref SP_DEVICE_INTERFACE_DETAIL_DATA DeviceInterfaceDetailData,
        uint DeviceInterfaceDetailDataSize, out uint RequiredSize,
        IntPtr DeviceInfoData);
    [DllImport("setupapi.dll")]
    static extern bool SetupDiDestroyDeviceInfoList(IntPtr DeviceInfoSet);

    // Kernel32
    [DllImport("kernel32.dll", CharSet=CharSet.Auto, SetLastError=true)]
    static extern IntPtr CreateFile(string lpFileName, uint dwDesiredAccess,
        uint dwShareMode, IntPtr lpSecurityAttributes, uint dwCreationDisposition,
        uint dwFlagsAndAttributes, IntPtr hTemplateFile);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool DeviceIoControl(IntPtr hDevice, uint dwIoControlCode,
        byte[] lpInBuffer, uint nInBufferSize,
        byte[] lpOutBuffer, uint nOutBufferSize,
        out uint lpBytesReturned, IntPtr lpOverlapped);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool CloseHandle(IntPtr hObject);

    static Guid WPD_GUID = new Guid("{6AC27878-A6FA-4155-BA85-F98F491D4F33}");

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
        b[16]=(byte)p;b[17]=(byte)(p>>8);b[18]=(byte)(p>>16);b[19]=(byte)(p>>24);
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

    // Find the WPD device path via SetupDi
    static string FindDevicePath() {
        const uint DIGCF_PRESENT=0x02, DIGCF_DEVICEINTERFACE=0x10;
        Guid g=WPD_GUID;
        IntPtr devInfo=SetupDiGetClassDevs(ref g,IntPtr.Zero,IntPtr.Zero,
            DIGCF_PRESENT|DIGCF_DEVICEINTERFACE);
        if(devInfo==new IntPtr(-1))return null;
        try {
            for(uint idx=0;;idx++) {
                var ifData=new SP_DEVICE_INTERFACE_DATA();
                ifData.cbSize=(uint)Marshal.SizeOf(typeof(SP_DEVICE_INTERFACE_DATA));
                if(!SetupDiEnumDeviceInterfaces(devInfo,IntPtr.Zero,ref g,idx,ref ifData))break;
                uint req=0;
                var detail=new SP_DEVICE_INTERFACE_DETAIL_DATA();
                detail.cbSize=(uint)(IntPtr.Size==8?8:5);
                SetupDiGetDeviceInterfaceDetail(devInfo,ref ifData,ref detail,512,out req,IntPtr.Zero);
                if(detail.DevicePath!=null&&detail.DevicePath.ToLower().Contains("vid_33a2"))
                    return detail.DevicePath;
            }
        } finally { SetupDiDestroyDeviceInfoList(devInfo); }
        return null;
    }

    // Send raw MTP command via DeviceIoControl and return response data
    static byte[] SendRawMtp(string devPath, ushort opCode, uint[] cmdParams) {
        const uint GENERIC_READ=0x80000000, GENERIC_WRITE=0x40000000;
        const uint FILE_SHARE_READ=0x01, FILE_SHARE_WRITE=0x02;
        const uint OPEN_EXISTING=3;
        // IOCTL_MTP_CUSTOM_COMMAND = CTL_CODE(FILE_DEVICE_UNKNOWN=0x22,0x803,METHOD_BUFFERED,FILE_ANY_ACCESS)
        const uint IOCTL_MTP_CUSTOM_COMMAND=0x0022200C;

        IntPtr hDev=CreateFile(devPath,GENERIC_READ|GENERIC_WRITE,
            FILE_SHARE_READ|FILE_SHARE_WRITE,IntPtr.Zero,OPEN_EXISTING,0,IntPtr.Zero);
        if(hDev==new IntPtr(-1)) {
            int err=Marshal.GetLastWin32Error();
            throw new Exception("CreateFile err="+err);
        }
        try {
            // MTP custom command input:
            // OpCode(2) + NumParams(2) + Params[N*4] + TransactionID(4)
            int nParams=cmdParams!=null?cmdParams.Length:0;
            byte[] inBuf=new byte[8+nParams*4];
            inBuf[0]=(byte)(opCode&0xFF); inBuf[1]=(byte)(opCode>>8);
            inBuf[2]=(byte)nParams; inBuf[3]=0;
            for(int k=0;k<nParams;k++) {
                byte[] pb=BitConverter.GetBytes(cmdParams[k]);
                Array.Copy(pb,0,inBuf,4+k*4,4);
            }
            inBuf[4+nParams*4]=0x01; // TransactionID

            byte[] outBuf=new byte[131072]; // 128KB
            uint returned=0;
            bool ok=DeviceIoControl(hDev,IOCTL_MTP_CUSTOM_COMMAND,
                inBuf,(uint)inBuf.Length,outBuf,(uint)outBuf.Length,
                out returned,IntPtr.Zero);
            if(!ok) {
                int err=Marshal.GetLastWin32Error();
                throw new Exception("DeviceIoControl err="+err);
            }
            byte[] result=new byte[returned];
            Array.Copy(outBuf,result,returned);
            return result;
        } finally { CloseHandle(hDev); }
    }

    static int Main(string[] args) {
        CoInitializeEx(IntPtr.Zero,0);
        string cmd=args.Length>0?args[0]:"list";
        uint propCode=args.Length>1?Convert.ToUInt32(args[1],16):0;
        string valueHex=args.Length>2?args[2]:"";

        try {
            string wpdId=GetWpdId();
            if(wpdId==null&&cmd!="rawmtp") {
                Console.WriteLine("{\"ok\":false,\"error\":\"No WPD device\"}");
                return 0;
            }
            if(cmd!="rawmtp") Console.Error.WriteLine("wpdId="+wpdId);

            if(cmd=="list") {
                Console.WriteLine("{\"ok\":true,\"device\":\""+Esc(wpdId)+"\"}");

            } else if(cmd=="get") {
                var ci=MakeClientInfo();
                IntPtr pDev=OpenDeviceRaw(wpdId,ci);
                object devObj=Marshal.GetObjectForIUnknown(pDev);
                var dev=(IPortableDevice)devObj;
                object contentObj; dev.Content(out contentObj);
                var content=(IPortableDeviceContent)contentObj;
                object propsObj; content.Properties(out propsObj);
                var props=(IPortableDeviceProperties)propsObj;

                object allValues; props.GetValues("DEVICE",null,out allValues);
                var vals=(IPortableDeviceValues)allValues;
                uint vcount; vals.GetCount(out vcount);
                Console.Error.WriteLine("DEVICE property count="+vcount);

                string OLY="4D545058-8900-40B3-8F1D-DC246E1E8370";
                var sb=new StringBuilder("{\"ok\":true,\"count\":"+vcount+",\"props\":[");
                for(uint vi=0;vi<vcount;vi++) {
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
                string OLY="4D545058-8900-40B3-8F1D-DC246E1E8370";
                var ci=MakeClientInfo();
                IntPtr pDev=OpenDeviceRaw(wpdId,ci);
                object devObj=Marshal.GetObjectForIUnknown(pDev);
                var dev=(IPortableDevice)devObj;
                object contentObj; dev.Content(out contentObj);
                var content=(IPortableDeviceContent)contentObj;
                object propsObj; content.Properties(out propsObj);
                var props=(IPortableDeviceProperties)propsObj;
                object keysObj=CreateObj("DE2D022D-2480-43BE-97F0-D1FA2CF98F4F",
                                          "DADA2357-E0AD-492E-98DB-DD61C53BA353");
                var keyColl=(IPortableDeviceKeyCollection)keysObj;
                IntPtr kPtr=MK(OLY,(uint)propCode);
                keyColl.Add(kPtr); Marshal.FreeHGlobal(kPtr);
                object valuesObj; props.GetValues("DEVICE",keyColl,out valuesObj);
                var values=(IPortableDeviceValues)valuesObj;
                IntPtr kPtr2=MK(OLY,(uint)propCode);
                byte[] result=new byte[8];
                try {
                    uint uval=0; values.GetUnsignedIntegerValue(kPtr2,out uval);
                    result=BitConverter.GetBytes(uval);
                    Console.Error.WriteLine("uint value="+uval+" (0x"+uval.ToString("X")+")");
                } catch {
                    try {
                        ulong ulval=0; values.GetUnsignedLargeIntegerValue(kPtr2,out ulval);
                        result=BitConverter.GetBytes(ulval);
                    } catch(Exception exV) {
                        Console.Error.WriteLine("GetValue failed: "+exV.Message);
                    }
                }
                Marshal.FreeHGlobal(kPtr2);
                CloseDeviceRaw(pDev);
                Console.WriteLine("{\"ok\":true,\"value\":\""+Convert.ToBase64String(result)+"\"}");

            } else if(cmd=="setprop") {
                string OLY="4D545058-8900-40B3-8F1D-DC246E1E8370";
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

            } else if(cmd=="scanifs") {
                // Scan all device interfaces for vid_33a2 across many GUIDs
                // to find the one that accepts IOCTL_MTP_CUSTOM_COMMAND
                const uint DIGCF_PRESENT=0x02, DIGCF_DEVICEINTERFACE=0x10;
                const uint GENERIC_READ=0x80000000, GENERIC_WRITE=0x40000000;
                const uint FILE_SHARE_READ=0x01, FILE_SHARE_WRITE=0x02;
                const uint OPEN_EXISTING=3;
                const uint IOCTL_MTP_CUSTOM_COMMAND=0x0022200C;

                // Known interface GUIDs to try
                Guid[] guids = new Guid[] {
                    new Guid("{6AC27878-A6FA-4155-BA85-F98F491D4F33}"), // WPD
                    new Guid("{EF9D1EB5-C8A8-4739-B1FC-D14C0AC17A88}"), // MTP
                    new Guid("{E6F07B5F-EE97-4A90-B076-33F57BF4EAA7}"), // WPDMTP
                    new Guid("{88BAE032-5A81-49F0-BC3D-A4FF138216D6}"), // WPDMTP2
                    new Guid("{36FC9E60-C465-11CF-8056-444553540000}"), // USB hub
                    new Guid("{A5DCBF10-6530-11D2-901F-00C04FB951ED}"), // USB device
                    new Guid("{53F56307-B6BF-11D0-94F2-00A0C91EFB8B}"), // disk
                    new Guid("{F18A0E88-C30C-11D0-8815-00A0C906BED8}"), // media
                };

                var sb = new StringBuilder("{\"ok\":true,\"interfaces\":[");
                bool first = true;
                foreach(Guid g in guids) {
                    Guid gCopy=g;
                    IntPtr devInfo=SetupDiGetClassDevs(ref gCopy,IntPtr.Zero,
                        IntPtr.Zero,DIGCF_PRESENT|DIGCF_DEVICEINTERFACE);
                    if(devInfo==new IntPtr(-1)) continue;
                    try {
                        for(uint idx=0;;idx++) {
                            var ifData=new SP_DEVICE_INTERFACE_DATA();
                            ifData.cbSize=(uint)Marshal.SizeOf(typeof(SP_DEVICE_INTERFACE_DATA));
                            if(!SetupDiEnumDeviceInterfaces(devInfo,IntPtr.Zero,ref gCopy,idx,ref ifData))break;
                            uint req=0;
                            var detail=new SP_DEVICE_INTERFACE_DETAIL_DATA();
                            detail.cbSize=(uint)(IntPtr.Size==8?8:5);
                            SetupDiGetDeviceInterfaceDetail(devInfo,ref ifData,ref detail,512,out req,IntPtr.Zero);
                            string path=detail.DevicePath;
                            if(path==null||!path.ToLower().Contains("vid_33a2")) continue;

                            // Try opening and sending IOCTL
                            IntPtr hDev=CreateFile(path,GENERIC_READ|GENERIC_WRITE,
                                FILE_SHARE_READ|FILE_SHARE_WRITE,IntPtr.Zero,OPEN_EXISTING,0,IntPtr.Zero);
                            string status="open_failed("+Marshal.GetLastWin32Error()+")";
                            if(hDev!=new IntPtr(-1)) {
                                byte[] inBuf=new byte[8];
                                inBuf[0]=0x86; inBuf[1]=0x94; // 0x9486
                                byte[] outBuf=new byte[256];
                                uint returned=0;
                                bool ok2=DeviceIoControl(hDev,IOCTL_MTP_CUSTOM_COMMAND,
                                    inBuf,(uint)inBuf.Length,outBuf,(uint)outBuf.Length,
                                    out returned,IntPtr.Zero);
                                status=ok2?"IOCTL_OK("+returned+"bytes)":"ioctl_err("+Marshal.GetLastWin32Error()+")";
                                CloseHandle(hDev);
                            }

                            if(!first) sb.Append(",");
                            sb.Append("{\"guid\":\""+gCopy+"\",\"path\":\""+Esc(path)+"\",\"status\":\""+status+"\"}");
                            first=false;
                        }
                    } finally { SetupDiDestroyDeviceInfoList(devInfo); }
                }
                sb.Append("]}");
                Console.WriteLine(sb.ToString());

            } else if(cmd=="probeioctls") {
                // Try different IOCTL codes on the USB device interface
                string usbPath="\\\\?\\usb#vid_33a2&pid_0136#bjra45526#{a5dcbf10-6530-11d2-901f-00c04fb951ed}";
                const uint GENERIC_READ=0x80000000,GENERIC_WRITE=0x40000000;
                const uint FILE_SHARE_READ=0x01,FILE_SHARE_WRITE=0x02,OPEN_EXISTING=3;
                IntPtr hDev=CreateFile(usbPath,GENERIC_READ|GENERIC_WRITE,
                    FILE_SHARE_READ|FILE_SHARE_WRITE,IntPtr.Zero,OPEN_EXISTING,0,IntPtr.Zero);
                if(hDev==new IntPtr(-1)) {
                    Console.WriteLine("{\"ok\":false,\"error\":\"open err="+Marshal.GetLastWin32Error()+"\"}");
                    return 0;
                }
                Console.Error.WriteLine("opened USB device");
                byte[] inBuf=new byte[8]; inBuf[0]=0x86; inBuf[1]=0x94;
                byte[] outBuf=new byte[65536];
                uint returned=0;
                var hits=new System.Collections.Generic.List<string>();
                uint[] codes=new uint[]{
                    0x00220000,0x00220004,0x00220008,0x0022000C,
                    0x00222000,0x00222004,0x00222008,0x0022200C,
                    0x00224000,0x00224004,0x00224008,0x0022400C,
                    0x00400000,0x00400004,0x00400008,0x0040000C,
                    0x00440000,0x00440004,
                };
                foreach(uint code in codes) {
                    returned=0;
                    bool ok2=DeviceIoControl(hDev,code,inBuf,(uint)inBuf.Length,
                        outBuf,(uint)outBuf.Length,out returned,IntPtr.Zero);
                    int err=Marshal.GetLastWin32Error();
                    if(ok2) hits.Add("OK:0x"+code.ToString("X8")+"("+returned+"b)");
                    else if(err!=1) hits.Add("ERR"+err+":0x"+code.ToString("X8"));
                }
                CloseHandle(hDev);
                Console.WriteLine("{\"ok\":true,\"hits\":\""+string.Join("|",hits)+"\"}");

            } else if(cmd=="getallowed") {
                string OLY="4D545058-8900-40B3-8F1D-DC246E1E8370";
                string ATTR="AB7943D8-6332-445F-A00D-8D5EF1E96F37";
                var ci=MakeClientInfo();
                IntPtr pDev=OpenDeviceRaw(wpdId,ci);
                object devObj=Marshal.GetObjectForIUnknown(pDev);
                var dev=(IPortableDevice)devObj;
                object contentObj; dev.Content(out contentObj);
                var content=(IPortableDeviceContent)contentObj;
                object propsObj; content.Properties(out propsObj);
                var props=(IPortableDeviceProperties)propsObj;
                IntPtr kPtr=MK(OLY,(uint)propCode);
                object attrsObj=null;
                try { props.GetPropertyAttributes("DEVICE",kPtr,out attrsObj); }
                catch(Exception exA) { Console.Error.WriteLine("GetPropertyAttributes: "+exA.Message); }
                Marshal.FreeHGlobal(kPtr);
                var sb=new StringBuilder("{\"ok\":true,\"values\":[");
                bool firstVal=true;
                if(attrsObj!=null) {
                    var attrs=(IPortableDeviceValues)attrsObj;
                    IntPtr kForm=MK(ATTR,2); uint form=0;
                    try { attrs.GetUnsignedIntegerValue(kForm,out form); } catch{}
                    Marshal.FreeHGlobal(kForm);
                    Console.Error.WriteLine("form="+form);
                    if(form==2) {
                        IntPtr kEnum=MK(ATTR,11); object enumColl=null;
                        try { attrs.GetIUnknownValue(kEnum,out enumColl); }
                        catch(Exception exE) { Console.Error.WriteLine("GetEnumColl: "+exE.Message); }
                        Marshal.FreeHGlobal(kEnum);
                        if(enumColl!=null) {
                            IntPtr pColl=Marshal.GetIUnknownForObject(enumColl);
                            IntPtr vtColl=Marshal.ReadIntPtr(pColl);
                            var getCountFn=(GetCollCountFn)Marshal.GetDelegateForFunctionPointer(
                                Marshal.ReadIntPtr(vtColl,3*IntPtr.Size),typeof(GetCollCountFn));
                            var getAtFn=(GetCollAtFn)Marshal.GetDelegateForFunctionPointer(
                                Marshal.ReadIntPtr(vtColl,4*IntPtr.Size),typeof(GetCollAtFn));
                            uint elemCount=0; getCountFn(pColl,out elemCount);
                            Console.Error.WriteLine("enum count="+elemCount);
                            for(uint ei=0;ei<elemCount;ei++) {
                                IntPtr pvPtr=Marshal.AllocHGlobal(16);
                                for(int bi=0;bi<16;bi++) Marshal.WriteByte(pvPtr,bi,0);
                                getAtFn(pColl,ei,pvPtr);
                                byte[] pvB=new byte[16]; Marshal.Copy(pvPtr,pvB,0,16);
                                Marshal.FreeHGlobal(pvPtr);
                                ushort vt=BitConverter.ToUInt16(pvB,0);
                                uint uval=(vt==18)?BitConverter.ToUInt16(pvB,8):BitConverter.ToUInt32(pvB,8);
                                if(!firstVal) sb.Append(",");
                                sb.Append(uval); firstVal=false;
                            }
                            Marshal.Release(pColl);
                        }
                    }
                }
                sb.Append("]}");
                CloseDeviceRaw(pDev);
                Console.WriteLine(sb.ToString());

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


def send_raw_mtp(op_code, params=None):
    """Send a raw MTP vendor command bypassing WPD."""
    hex_params = ""
    if params:
        hex_params = "".join(f"{p:08X}" for p in params)
    return _bridge(["rawmtp", f"{op_code:04X}", hex_params] if hex_params else ["rawmtp", f"{op_code:04X}"])


def parse_9486_response(data_b64):
    """
    Parse the 0x9486 GetAllDevicePropDesc response.
    Returns dict of {prop_code: current_value_bytes}
    """
    data = base64.b64decode(data_b64)
    result = {}
    type_sizes = {0x0002:1, 0x0004:2, 0x0006:4, 0x0008:8,
                  0x0003:1, 0x0005:2, 0x0007:4, 0x0009:8}
    # Skip 4-byte header
    p = 4
    while p + 6 <= len(data):
        try:
            prop_code = struct.unpack_from('<H', data, p)[0]
            data_type = struct.unpack_from('<H', data, p+2)[0]
            get_set   = data[p+4]
            val_size  = type_sizes.get(data_type, 2)
            if p + 5 + val_size*2 + 1 > len(data):
                break
            # Default value at p+5, Current value at p+5+val_size
            cur_bytes = data[p+5+val_size : p+5+val_size*2]
            result[prop_code] = cur_bytes
            form_flag = data[p+5+val_size*2]
            base_next = p + 5 + val_size*2 + 1
            if form_flag == 0:
                p = base_next
            elif form_flag == 1:
                p = base_next + val_size*3
            elif form_flag == 2:
                if base_next + 2 > len(data): break
                n = struct.unpack_from('<H', data, base_next)[0]
                p = base_next + 2 + n*val_size
            else:
                p = base_next
        except Exception:
            break
    return result


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
    print("Step 2: Probe IOCTL codes on USB device interface...")
    r2 = _bridge(["probeioctls"])
    print(f"  {r2}")
    r2 = send_raw_mtp(0x9486)
    print(f"  ok={r2.get('ok')} returned={r2.get('returned', 0)} bytes")

    if r2.get("ok") and r2.get("data"):
        data_bytes = base64.b64decode(r2["data"])
        print(f"  Raw first 32 bytes: {data_bytes[:32].hex()}")
        props = parse_9486_response(r2["data"])
        print(f"  Parsed {len(props)} property descriptors")
        for code in [0xD002, 0xD01C, 0xD008]:
            if code in props:
                raw = props[code]
                val = decode_value(code, raw)
                print(f"  0x{code:04X}: raw={raw.hex()} decoded={val}")
            else:
                print(f"  0x{code:04X}: not found")
    else:
        print(f"  Error: {r2.get('error', 'unknown')}")
