param([string]$name = "shot", [string]$type = "", [switch]$Click)
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
using System.Text;
public class Win {
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr FindWindowW(string c, string n);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern IntPtr GetWindow(IntPtr h, uint g);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr SendMessageW(IntPtr h, int m, IntPtr w, string l);
  [DllImport("user32.dll")] public static extern int GetWindowTextLengthW(IntPtr h);
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
}
"@
[Win]::SetProcessDPIAware() | Out-Null
$h = [Win]::FindWindowW("AnvilFletchWindow", "Anvil Fletch")
if ($h -eq [IntPtr]::Zero) { Write-Output "no window"; exit 1 }
[Win]::SetForegroundWindow($h) | Out-Null
Start-Sleep -Milliseconds 400

if ($type -ne "") {
  $edit = [Win]::GetWindow($h, 5)   # GW_CHILD
  [Win]::SendMessageW($edit, 0x000C, 0, $type) | Out-Null   # WM_SETTEXT
}

$b = New-Object Win+RECT
[Win]::GetWindowRect($h, [ref]$b) | Out-Null
$x, $y = $b.L, $b.T
$w = [Math]::Max(1, $b.R - $b.L)
$hh = [Math]::Max(1, $b.B - $b.T)

$bmp = New-Object System.Drawing.Bitmap($w, $hh)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($x, $y, 0, 0, (New-Object System.Drawing.Size($w, $hh)))
$out = Join-Path $PSScriptRoot ("..\bin\view_{0}.png" -f $name)
$bmp.Save($out)
$g.Dispose(); $bmp.Dispose()
Write-Output "saved $out ($w x $hh)"
