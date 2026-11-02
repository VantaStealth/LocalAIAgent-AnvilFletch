param([string]$src = "assets\anvil_icon_source.png", [string]$out = "bin\anvil.ico")
Add-Type -AssemblyName System.Drawing
$root = Split-Path $PSScriptRoot -Parent
$srcPath = Join-Path $root $src
$outPath = Join-Path $root $out
$img = [System.Drawing.Image]::FromFile($srcPath)

$sizes = @(16, 24, 32, 48, 64, 128, 256)
$pngs = @()
foreach ($s in $sizes) {
  $bmp = New-Object System.Drawing.Bitmap($s, $s, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
  $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
  $g.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
  $g.Clear([System.Drawing.Color]::Transparent)
  $g.DrawImage($img, 0, 0, $s, $s)
  $g.Dispose()

  $r = [Math]::Max(4.0, [Math]::Round($s * 0.225))
  $rect = New-Object System.Drawing.RectangleF(0.45, 0.45, ($s - 0.9), ($s - 0.9))
  $path = New-Object System.Drawing.Drawing2D.GraphicsPath
  $d = $r * 2
  $path.AddArc($rect.X, $rect.Y, $d, $d, 180, 90)
  $path.AddArc($rect.Right - $d, $rect.Y, $d, $d, 270, 90)
  $path.AddArc($rect.Right - $d, $rect.Bottom - $d, $d, $d, 0, 90)
  $path.AddArc($rect.X, $rect.Bottom - $d, $d, $d, 90, 90)
  $path.CloseFigure()

  $data = $bmp.LockBits((New-Object System.Drawing.Rectangle(0,0,$s,$s)),
    [System.Drawing.Imaging.ImageLockMode]::ReadWrite,
    [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
  $buf = New-Object byte[] ($data.Stride * $s)
  [System.Runtime.InteropServices.Marshal]::Copy($data.Scan0, $buf, 0, $buf.Length)
  for ($y = 0; $y -lt $s; $y++) {
    for ($x = 0; $x -lt $s; $x++) {
      $inside = $path.IsVisible([single]$x + 0.5, [single]$y + 0.5)
      $i = $y * $data.Stride + $x * 4
      if (-not $inside) { $buf[$i] = 0; $buf[$i+1] = 0; $buf[$i+2] = 0; $buf[$i+3] = 0 }
    }
  }
  [System.Runtime.InteropServices.Marshal]::Copy($buf, 0, $data.Scan0, $buf.Length)
  $bmp.UnlockBits($data)

  $ms = New-Object System.IO.MemoryStream
  $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
  $pngs += ,($ms.ToArray())
  $ms.Dispose(); $bmp.Dispose(); $path.Dispose()
}
$img.Dispose()

$fs = New-Object System.IO.MemoryStream
$bw = New-Object System.IO.BinaryWriter($fs)
$bw.Write([uint16]0); $bw.Write([uint16]1); $bw.Write([uint16]$sizes.Count)
$offset = 6 + 16 * $sizes.Count
for ($k = 0; $k -lt $sizes.Count; $k++) {
  $s = $sizes[$k]; $bytes = $pngs[$k]
  $dim = if ($s -ge 256) { 0 } else { $s }
  $bw.Write([byte]$dim); $bw.Write([byte]$dim)
  $bw.Write([byte]0); $bw.Write([byte]0)
  $bw.Write([uint16]1); $bw.Write([uint16]32)
  $bw.Write([uint32]$bytes.Length); $bw.Write([uint32]$offset)
  $offset += $bytes.Length
}
foreach ($b in $pngs) { $bw.Write($b) }
$bw.Flush()
[System.IO.File]::WriteAllBytes($outPath, $fs.ToArray())
$bw.Dispose(); $fs.Dispose()
Write-Output "wrote $outPath ($($sizes -join ','))"
