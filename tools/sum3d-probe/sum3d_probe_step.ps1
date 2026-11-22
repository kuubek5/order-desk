<#
  One call into Sum3dDll_x64.dll, run in its own child process by sum3d_probe.ps1.
  If the DLL crashes or shows a dialog, only this process dies (the parent kills it on timeout).
  Prints exactly one JSON line to stdout. Never writes to Sum3D files itself.
  Level 1 steps only read. Level 2 steps (kit v4: import, tocam) WRITE, but only the output
  files the parent names inside its own work folder (C:\sum3d_probe\<date>\work), from copies.
#>
param(
    [Parameter(Mandatory = $true)][string]$SumDir,
    [Parameter(Mandatory = $true)][ValidateSet('load', 'init', 'machines', 'strategy', 'space', 'curves',
            'equipment', 'import', 'tocam')][string]$Step,
    [string]$Cam = '',
    # Working directory of this process. Default: our own folder, NOT the Sum3D folder, because
    # Sum3D uses fixed temp-file names (FILEPOST.TMP, filetemp.tmp, ...) that open windows may be using.
    [string]$WorkDir = '',
    # Level 2 (kit v4): put one STL on a COPY of a disc .blk → .obj → .cam (all in the work folder).
    [string]$Machine = '',
    [string]$Material = '',
    [string]$Equipment = '',
    [string]$Blk = '',
    [string]$Stl = '',
    [string]$Obj = '',
    [int]$ObjectType = 9,
    [double]$Offset = 0.001,
    [double]$MeshAngle = 0.0,
    [double]$Shrink = 1.25
)

$ErrorActionPreference = 'Stop'
$result = [ordered]@{ step = $Step; ok = $false }

function Emit($r) { ($r | ConvertTo-Json -Compress -Depth 5) | Write-Output }

if (-not [Environment]::Is64BitProcess) {
    $result.error = 'need 64-bit PowerShell'
    Emit $result; exit 3
}

$source = @'
using System;
using System.Runtime.InteropServices;

public static class Kernel {
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern bool SetDllDirectory(string path);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern IntPtr LoadLibrary(string path);
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern IntPtr GlobalFree(IntPtr h);
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool GlobalUnlock(IntPtr h);
}

// Decorated (C++) export names of Sum3dDll_x64.dll; signatures come from the decoration itself.
public static class Sum3d {
    const string Dll = "Sum3dDll_x64.dll";

    [DllImport(Dll, EntryPoint = "?setSumDirectoryInst@@YAXPEBD@Z", CallingConvention = CallingConvention.Cdecl, CharSet = CharSet.Ansi)]
    public static extern void setSumDirectoryInst(string dir);

    [DllImport(Dll, EntryPoint = "?initialize@@YA?AW4ISum3dDLLErrorCode@ISum3dDll@@XZ", CallingConvention = CallingConvention.Cdecl)]
    public static extern int initialize();

    [DllImport(Dll, EntryPoint = "?getMachineNameList@@YA_NPEADH@Z", CallingConvention = CallingConvention.Cdecl)]
    [return: MarshalAs(UnmanagedType.I1)]
    public static extern bool getMachineNameList(byte[] buf, int len);

    [DllImport(Dll, EntryPoint = "?getUsedStrategyName@@YA?AW4ISum3dDLLErrorCode@ISum3dDll@@PEBDPEAD@Z", CallingConvention = CallingConvention.Cdecl, CharSet = CharSet.Ansi)]
    public static extern int getUsedStrategyName(string cam, byte[] outName);

    [DllImport(Dll, EntryPoint = "?getSpaceLeftOnRAW@@YAHPEBD@Z", CallingConvention = CallingConvention.Cdecl, CharSet = CharSet.Ansi)]
    public static extern int getSpaceLeftOnRAW(string cam);

    [DllImport(Dll, EntryPoint = "?getSpaceLeftOnRAW_WithCurves@@YAHPEBDAEAHAEAPEAXAEAPEAN@Z", CallingConvention = CallingConvention.Cdecl, CharSet = CharSet.Ansi)]
    public static extern int getSpaceLeftOnRAW_WithCurves(string cam, ref int count, ref IntPtr hglobal, ref IntPtr values);

    // Equipment (*.eqp) of one machine: "default|<name>|..." ; buf too small -> len = needed size.
    [DllImport(Dll, EntryPoint = "?getEquipmentList@@YA?AW4ISum3dDLLErrorCode@ISum3dDll@@PEB_WPEADAEAH@Z", CallingConvention = CallingConvention.Cdecl, CharSet = CharSet.Unicode)]
    public static extern int getEquipmentList(string machine, byte[] buf, ref int len);

    // importSTL, 19-argument overload (decoded from the decoration and from the DLL's own importstl.log):
    // machine, material, equipment, block .blk, object type, offset (>= 0.001), rotation 180,
    // mesh-size angle, undercut optimisation, shrinking factor, premilled index (must be >= 0),
    // extern undercut, prepline generation, prepline width, margin detection, connection mesh
    // separation, NO-premilled flag (true -> premilled position -1), STL in, OBJ out.
    [DllImport(Dll, EntryPoint = "?importSTL@@YA?AW4ISum3dDLLErrorCode@ISum3dDll@@PEBD000HN_NNHNH11N11100@Z", CallingConvention = CallingConvention.Cdecl, CharSet = CharSet.Ansi)]
    public static extern int importSTL(string machine, string material, string equipment, string blk,
        int objectType, double offset, [MarshalAs(UnmanagedType.I1)] bool rotate180, double meshAngle,
        int undercut, double shrink, int premilled, [MarshalAs(UnmanagedType.I1)] bool externUndercut,
        [MarshalAs(UnmanagedType.I1)] bool prepline, double preplineWidth, [MarshalAs(UnmanagedType.I1)] bool margin,
        [MarshalAs(UnmanagedType.I1)] bool connectionSeparation, [MarshalAs(UnmanagedType.I1)] bool noPremilled,
        string stlIn, string objOut);

    // MillBox .obj -> Sum3D .cam (converts and copies; the DLL deletes its own temp file).
    [DllImport(Dll, EntryPoint = "?MB2Sum_MBobj2SUMcam@@YA_NPEBDPEAD@Z", CallingConvention = CallingConvention.Cdecl, CharSet = CharSet.Ansi)]
    [return: MarshalAs(UnmanagedType.I1)]
    public static extern bool MBobj2SUMcam(string obj, byte[] camOut);
}
'@

function BufText([byte[]]$buf) {
    # Strings come back in the ANSI code page; NUL separators shown as '|'.
    $end = $buf.Length
    while ($end -gt 0 -and $buf[$end - 1] -eq 0) { $end-- }
    if ($end -eq 0) { return '' }
    $text = [Text.Encoding]::Default.GetString($buf, 0, $end)
    return $text.Replace([char]0, '|')
}

# Level 2 writes ONLY new files outside the Sum3D folder: never over an existing file, never into Sum3D.
function Assert-OutputPath([string]$path) {
    if (-not $path) { throw 'output path is empty' }
    $full = [IO.Path]::GetFullPath($path)
    $sum = [IO.Path]::GetFullPath($SumDir).TrimEnd('\') + '\'
    if ($full.StartsWith($sum, [StringComparison]::OrdinalIgnoreCase)) { throw "refuse to write inside the Sum3D folder: $full" }
    if (Test-Path -LiteralPath $full) { throw "refuse to overwrite an existing file: $full" }
}

try {
    Add-Type -TypeDefinition $source -Language CSharp
    $cwd = $(if ($WorkDir) { $WorkDir } else { $SumDir })
    Set-Location -LiteralPath $cwd
    [Environment]::CurrentDirectory = $cwd
    $result.cwd = $cwd
    [void][Kernel]::SetDllDirectory($SumDir)
    $dllPath = Join-Path $SumDir 'Sum3dDll_x64.dll'

    $h = [Kernel]::LoadLibrary($dllPath)
    if ($h -eq [IntPtr]::Zero) {
        $result.error = "LoadLibrary failed, win32 error $([Runtime.InteropServices.Marshal]::GetLastWin32Error())"
        Emit $result; exit 4
    }
    $result.loaded = $true
    if ($Step -eq 'load') { $result.ok = $true; Emit $result; exit 0 }

    [Sum3d]::setSumDirectoryInst($SumDir.TrimEnd('\') + '\')
    $code = [Sum3d]::initialize()
    $result.init_code = $code
    if ($Step -eq 'init') { $result.ok = ($code -eq 0); Emit $result; exit 0 }
    if ($code -ne 0) { $result.error = "initialize() returned $code"; Emit $result; exit 5 }

    switch ($Step) {
        'equipment' {
            $len = 65536
            $buf = New-Object byte[] $len
            $rc = [Sum3d]::getEquipmentList($Machine, $buf, [ref]$len)
            $result.code = $rc
            $result.ok = ($rc -eq 0)
            $result.equipment_raw = BufText $buf
        }
        'import' {
            foreach ($out in @($Obj)) { Assert-OutputPath $out }
            $rc = [Sum3d]::importSTL($Machine, $Material, $Equipment, $Blk, $ObjectType, $Offset, $false,
                $MeshAngle, 0, $Shrink, 0, $false, $false, 0.0, $false, $false, $true, $Stl, $Obj)
            $result.code = $rc
            $result.ok = ($rc -eq 0) -and (Test-Path -LiteralPath $Obj)
            if (Test-Path -LiteralPath $Obj) { $result.obj_size = (Get-Item -LiteralPath $Obj).Length }
        }
        'tocam' {
            Assert-OutputPath $Cam
            $bytes = [Text.Encoding]::Default.GetBytes($Cam + [char]0)
            $result.ok = [Sum3d]::MBobj2SUMcam($Obj, $bytes) -and (Test-Path -LiteralPath $Cam)
            if (Test-Path -LiteralPath $Cam) { $result.cam_size = (Get-Item -LiteralPath $Cam).Length }
        }
        'machines' {
            $buf = New-Object byte[] 65536
            $result.ok = [Sum3d]::getMachineNameList($buf, $buf.Length)
            $result.machines_raw = BufText $buf
        }
        'strategy' {
            $buf = New-Object byte[] 65536   # no length parameter in the API: keep it large
            $rc = [Sum3d]::getUsedStrategyName($Cam, $buf)
            $result.code = $rc
            $result.ok = ($rc -eq 0)
            $result.strategy = BufText $buf
        }
        'space' {
            $v = [Sum3d]::getSpaceLeftOnRAW($Cam)
            $result.value = $v
            $result.ok = ($v -ne -1)
        }
        'curves' {
            $n = 0; $hg = [IntPtr]::Zero; $p = [IntPtr]::Zero
            $v = [Sum3d]::getSpaceLeftOnRAW_WithCurves($Cam, [ref]$n, [ref]$hg, [ref]$p)
            $result.value = $v
            $result.count = $n
            if ($n -gt 0 -and $p -ne [IntPtr]::Zero) {
                $take = [Math]::Min($n, 24)
                $arr = New-Object double[] $take
                [Runtime.InteropServices.Marshal]::Copy($p, $arr, 0, $take)
                $result.first_values = $arr
            }
            if ($hg -ne [IntPtr]::Zero) { [void][Kernel]::GlobalUnlock($hg); [void][Kernel]::GlobalFree($hg) }
            $result.ok = ($v -ne -1)
        }
    }
    Emit $result
    exit 0
}
catch {
    $result.error = $_.Exception.Message
    Emit $result
    exit 6
}
