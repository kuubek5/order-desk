<#
  One read-only call into Sum3dDll_x64.dll, run in its own child process by sum3d_probe.ps1.
  If the DLL crashes or shows a dialog, only this process dies (the parent kills it on timeout).
  Prints exactly one JSON line to stdout. Never writes to Sum3D files itself.
#>
param(
    [Parameter(Mandatory = $true)][string]$SumDir,
    [Parameter(Mandatory = $true)][ValidateSet('load', 'init', 'machines', 'strategy', 'space', 'curves')][string]$Step,
    [string]$Cam = '',
    # Working directory of this process. Default: our own folder, NOT the Sum3D folder, because
    # Sum3D uses fixed temp-file names (FILEPOST.TMP, filetemp.tmp, ...) that open windows may be using.
    [string]$WorkDir = ''
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
