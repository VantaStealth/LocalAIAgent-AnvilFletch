param([string]$Path = "", [int]$Left = 0, [int]$Top = 0, [int]$Width = 0, [int]$Height = 0)
# Coordinates must match the DPI-aware backend (physical pixels), otherwise every
# click on a 125%/150% display lands short by the scale factor.
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class AFDPI {
  [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr v);
  [DllImport("shcore.dll")] public static extern int SetProcessDpiAwareness(int v);
}
"@ -ErrorAction SilentlyContinue
try { [AFDPI]::SetProcessDpiAwarenessContext([IntPtr]::new(-4)) | Out-Null } catch {
  try { [AFDPI]::SetProcessDpiAwareness(2) | Out-Null } catch { }
}
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Storage.StorageFile, Windows.Foundation, ContentType=WindowsRuntime]

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
  $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $type) {
  $asTask = $asTaskGeneric.MakeGenericMethod($type)
  $netTask = $asTask.Invoke($null, @($op))
  $netTask.Wait(-1) | Out-Null
  $netTask.Result
}

$isTemp = $false
if (-not $Path) {
  $isTemp = $true
  if ($Width -gt 0 -and $Height -gt 0) {
    $bmp = New-Object System.Drawing.Bitmap($Width, $Height)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.CopyFromScreen($Left, $Top, 0, 0, $bmp.Size)
    $g.Dispose()
  } else {
    $b = [System.Windows.Forms.SystemInformation]::VirtualScreen
    $bmp = New-Object System.Drawing.Bitmap($b.Width, $b.Height)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.CopyFromScreen($b.X, $b.Y, 0, 0, $bmp.Size)
    $g.Dispose()
    $Left = $b.X; $Top = $b.Y
  }
  $Path = Join-Path $env:TEMP ("af_ocr_" + [guid]::NewGuid().ToString("N") + ".png")
  $bmp.Save($Path, [System.Drawing.Imaging.ImageFormat]::Png)
  $bmp.Dispose()
} else {
  $Left = 0; $Top = 0
}
$OffsetX = $Left
$OffsetY = $Top

$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if (-not $engine) {
  try {
    $lang = New-Object Windows.Globalization.Language("en-US")
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
  } catch { }
}
if (-not $engine) { Write-Output "NO_OCR_ENGINE"; exit 2 }
$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
$out = @()
foreach ($line in $result.Lines) {
  $l = $null; $t = $null; $r = $null; $b = $null
  foreach ($w in $line.Words) {
    $rc = $w.BoundingRect
    if ($null -eq $l -or $rc.X -lt $l) { $l = $rc.X }
    if ($null -eq $t -or $rc.Y -lt $t) { $t = $rc.Y }
    $rr = $rc.X + $rc.Width; $bb = $rc.Y + $rc.Height
    if ($null -eq $r -or $rr -gt $r) { $r = $rr }
    if ($null -eq $b -or $bb -gt $b) { $b = $bb }
  }
  $text = ($line.Text -replace '[\x00-\x1F]', ' ').Trim()
  $out += [pscustomobject]@{
    text = $text
    x = [int]($l + $OffsetX); y = [int]($t + $OffsetY)
    w = [int]($r - $l); h = [int]($b - $t)
    cx = [int]($l + ($r - $l) / 2 + $OffsetX); cy = [int]($t + ($b - $t) / 2 + $OffsetY)
  }
}
if ($result.Lines.Count -eq 0) { Write-Output "[]" } else { $out | ConvertTo-Json -Compress }
if ($isTemp) { try { Remove-Item $Path -Force -ErrorAction SilentlyContinue } catch { } }
