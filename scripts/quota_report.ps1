& {
$dir = Join-Path $env:LOCALAPPDATA 'KuubMill\logs'
$out = Join-Path ([Environment]::GetFolderPath('Desktop')) 'kmill-quota-report.txt'
$files = @(Get-ChildItem $dir -Filter 'kuubmill.log*' -ErrorAction SilentlyContinue | Sort-Object LastWriteTime)
if ($files.Count -eq 0) { Write-Host "NO LOG FILES in $dir" -ForegroundColor Red; return }
$rxTs = [regex]'^(\d{4}-\d\d-\d\d) (\d\d):\d\d:\d\d'
$rxCount = [regex]'Sheets API: (\d+) '
$samples = New-Object System.Collections.Generic.List[object]
$skips = @{}; $quota = @{}; $proxy = @{}
$quotaLast = New-Object System.Collections.Generic.List[string]
$proxyLast = New-Object System.Collections.Generic.List[string]
$first = $null; $last = $null; $day = '?'; $total = 0
foreach ($f in $files) {
  $fs = New-Object IO.FileStream($f.FullName, 'Open', 'Read', 'ReadWrite')
  $sr = New-Object IO.StreamReader($fs, [Text.Encoding]::UTF8)
  while ($null -ne ($l = $sr.ReadLine())) {
    $total++
    $m = $rxTs.Match($l)
    if ($m.Success) {
      $day = $m.Groups[1].Value; $hour = $m.Groups[2].Value
      if (-not $first) { $first = $l.Substring(0, 19) }
      $last = $l.Substring(0, 19)
      $c = $rxCount.Match($l)
      if ($c.Success) { $samples.Add([pscustomobject]@{ Day = $day; Hour = $hour; N = [int]$c.Groups[1].Value }); continue }
      if ($l -match '\u0434\u043E Sheets') { $skips[$day] = 1 + [int]$skips[$day]; continue }
    }
    if ($l -match 'Quota exceeded') {
      $quota[$day] = 1 + [int]$quota[$day]
      $quotaLast.Add($l.Substring(0, [Math]::Min(250, $l.Length)))
    } elseif ($l -match '\b429\b' -and $l -match '(?i)sheet|google|gspread|APIError') {
      $proxy[$day] = 1 + [int]$proxy[$day]
      $proxyLast.Add($l.Substring(0, [Math]::Min(250, $l.Length)))
    }
  }
  $sr.Close()
}
$r = New-Object System.Collections.Generic.List[string]
$r.Add("KMill quota report  PC=$env:COMPUTERNAME  made=$(Get-Date -Format 'yyyy-MM-dd HH:mm')")
$r.Add("log files=$($files.Count)  lines=$total  period: $first .. $last")
$r.Add('')
$r.Add('== Sheets API requests per minute (one sample per minute) ==')
if ($samples.Count -gt 0) {
  $st = $samples | Measure-Object N -Average -Maximum
  $r.Add(("samples={0}  avg={1:N1}  max={2}  minutes>=45: {3}  minutes>=60: {4}" -f $st.Count, $st.Average, $st.Maximum, @($samples | Where-Object { $_.N -ge 45 }).Count, @($samples | Where-Object { $_.N -ge 60 }).Count))
  $r.Add('-- per day: day  avg  max  minutes>=45')
  foreach ($g in ($samples | Group-Object Day | Sort-Object Name)) {
    $s = $g.Group | Measure-Object N -Average -Maximum
    $r.Add(("{0}  {1,5:N1}  {2,3}  {3}" -f $g.Name, $s.Average, $s.Maximum, @($g.Group | Where-Object { $_.N -ge 45 }).Count))
  }
  $r.Add('-- per hour of day (all days): hour  avg  max')
  foreach ($g in ($samples | Group-Object Hour | Sort-Object Name)) {
    $s = $g.Group | Measure-Object N -Average -Maximum
    $r.Add(("{0}h  {1,5:N1}  {2,3}" -f $g.Name, $s.Average, $s.Maximum))
  }
} else { $r.Add('no samples found') }
$r.Add('')
$r.Add('== Hot tick skipped by quota brake (per day) ==')
if ($skips.Count) { foreach ($k in ($skips.Keys | Sort-Object)) { $r.Add("$k  $($skips[$k])") } } else { $r.Add('none') }
$r.Add('')
$r.Add('== Google quota 429 lines "Quota exceeded" (per day) ==')
if ($quota.Count) { foreach ($k in ($quota.Keys | Sort-Object)) { $r.Add("$k  $($quota[$k])") }; $r.Add('-- last 3:'); $quotaLast | Select-Object -Last 3 | ForEach-Object { $r.Add($_) } } else { $r.Add('none') }
$r.Add('')
$r.Add('== Other 429 lines, not quota (proxy?) (per day) ==')
if ($proxy.Count) { foreach ($k in ($proxy.Keys | Sort-Object)) { $r.Add("$k  $($proxy[$k])") }; $r.Add('-- last 3:'); $proxyLast | Select-Object -Last 3 | ForEach-Object { $r.Add($_) } } else { $r.Add('none') }
$r | Out-File -FilePath $out -Encoding UTF8
Write-Host "DONE: $out" -ForegroundColor Green
notepad $out
}
