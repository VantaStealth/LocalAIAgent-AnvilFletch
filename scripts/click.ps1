param([string]$pts = "150,320", [int]$delayMs = 400, [string]$shot = "")
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class K1 {
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr FindWindowW(string c, string n);
  [DllImport("user32.dll")] public static extern IntPtr PostMessageW(IntPtr h, int m, IntPtr w, IntPtr l);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr hdc, uint flags);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
}
"@
[K1]::SetProcessDPIAware() | Out-Null
$h = [K1]::FindWindowW("AnvilFletchWindow", "Anvil Fletch")
if ($h -eq [IntPtr]::Zero) { Write-Output "no window"; exit 1 }
[K1]::SetForegroundWindow($h) | Out-Null
Start-Sleep -Milliseconds 300

foreach ($p in ($pts -split ";")) {
  $xy = $p -split ","
  $x = [int]$xy[0]; $y = [int]$xy[1]
  $lp = [IntPtr](($y -shl 16) -bor ($x -band 0xFFFF))
  [K1]::PostMessageW($h, 0x0200, [IntPtr]0, $lp) | Out-Null   # WM_MOUSEMOVE
  Start-Sleep -Milliseconds 120
  [K1]::PostMessageW($h, 0x0201, [IntPtr]1, $lp) | Out-Null   # WM_LBUTTONDOWN
  [K1]::PostMessageW($h, 0x0202, [IntPtr]0, $lp) | Out-Null   # WM_LBUTTONUP
  Start-Sleep -Milliseconds $delayMs
}

if ($shot -ne "") {
  Start-Sleep -Milliseconds 350
  $r = New-Object K1+RECT
  [K1]::GetWindowRect($h, [ref]$r) | Out-Null
  $w = $r.R - $r.L; $hh = $r.B - $r.T
  $bmp = New-Object System.Drawing.Bitmap($w, $hh)
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $hdc = $g.GetHdc()
  $ok = [K1]::PrintWindow($h, $hdc, 2)
  $g.ReleaseHdc($hdc)
  if (-not $ok) { $g.CopyFromScreen($r.L, $r.T, 0, 0, (New-Object System.Drawing.Size($w, $hh))) }
  $g.Dispose()
  $out = Join-Path $PSScriptRoot "..\bin\view_$shot.png"
  $bmp.Save($out); $bmp.Dispose()
  Write-Output "saved $out ($w x $hh)"
}
Write-Output "clicked $pts"
