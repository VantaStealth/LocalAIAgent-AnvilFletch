param([string]$prompt = "", [string]$shots = "a", [string]$delays = "3")
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class W2 {
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr FindWindowW(string c, string n);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern IntPtr GetWindow(IntPtr h, uint g);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr SendMessageW(IntPtr h, int m, IntPtr w, string l);
  [DllImport("user32.dll")] public static extern IntPtr PostMessageW(IntPtr h, int m, IntPtr w, IntPtr l);
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr hdc, uint flags);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
}
"@
[W2]::SetProcessDPIAware() | Out-Null
$h = [W2]::FindWindowW("AnvilFletchWindow", "Anvil Fletch")
if ($h -eq [IntPtr]::Zero) { Write-Output "no window"; exit 1 }
$edit = [W2]::GetWindow($h, 5)

function Shoot([string]$n) {
  $r = New-Object W2+RECT
  [W2]::GetWindowRect($h, [ref]$r) | Out-Null
  $w = [Math]::Max(1, $r.R - $r.L); $hh = [Math]::Max(1, $r.B - $r.T)
  $bmp = New-Object System.Drawing.Bitmap($w, $hh)
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $hdc = $g.GetHdc()
  $ok = [W2]::PrintWindow($h, $hdc, 2)
  $g.ReleaseHdc($hdc)
  if (-not $ok) { $g.CopyFromScreen($r.L, $r.T, 0, 0, (New-Object System.Drawing.Size($w, $hh))) }
  $g.Dispose()
  $out = Join-Path $PSScriptRoot "..\bin\view_$n.png"
  $bmp.Save($out); $bmp.Dispose()
  Write-Output "saved $out ($w x $hh)"
}

[W2]::SetForegroundWindow($h) | Out-Null
if ($prompt -ne "") {
  [W2]::SendMessageW($edit, 0x000C, [IntPtr]0, $prompt) | Out-Null
  Start-Sleep -Milliseconds 250
  [W2]::PostMessageW($edit, 0x0100, [IntPtr]13, [IntPtr]0) | Out-Null   # WM_KEYDOWN VK_RETURN
  [W2]::PostMessageW($edit, 0x0101, [IntPtr]13, [IntPtr]0) | Out-Null   # WM_KEYUP
}
$sn = $shots -split ","; $dl = $delays -split ","
for ($i = 0; $i -lt $sn.Count; $i++) {
  Start-Sleep -Seconds ([int]$dl[$i])
  Shoot $sn[$i]
}
