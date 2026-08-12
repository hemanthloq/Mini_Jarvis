# Generates jarvis.ico (arc-reactor style, cyan on dark) in the repo root.
# Pure System.Drawing — no Python/Pillow needed. Run:  powershell -File scripts\make_icon.ps1
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing

function New-JarvisBitmap([int]$size) {
    $bmp = New-Object System.Drawing.Bitmap($size, $size,
        [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $g.Clear([System.Drawing.Color]::Transparent)
    $g.ScaleTransform($size / 256.0, $size / 256.0)   # draw in a 256 space, scale down

    $cyan    = [System.Drawing.Color]::FromArgb(255, 34, 211, 238)
    $cyanDim = [System.Drawing.Color]::FromArgb(150, 34, 211, 238)
    $dark    = [System.Drawing.Color]::FromArgb(255, 9, 14, 22)
    $white   = [System.Drawing.Color]::FromArgb(255, 190, 247, 255)

    $g.FillEllipse((New-Object System.Drawing.SolidBrush($dark)), 6, 6, 244, 244)
    $g.DrawEllipse((New-Object System.Drawing.Pen($cyan, 12)), 24, 24, 208, 208)

    $penMid = New-Object System.Drawing.Pen($cyanDim, 16)
    for ($a = 0; $a -lt 360; $a += 45) { $g.DrawArc($penMid, 66, 66, 124, 124, ($a + 7), 31) }

    $g.FillEllipse((New-Object System.Drawing.SolidBrush($cyan)), 95, 95, 66, 66)
    $g.FillEllipse((New-Object System.Drawing.SolidBrush($white)), 111, 111, 34, 34)
    $g.Dispose()
    return $bmp
}

function Get-PngBytes($bmp) {
    $ms = New-Object System.IO.MemoryStream
    $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
    return ,$ms.ToArray()
}

$sizes = 16, 32, 48, 64, 128, 256
$pngs = @()
foreach ($sz in $sizes) { $b = New-JarvisBitmap $sz; $pngs += , (Get-PngBytes $b); $b.Dispose() }

# Assemble a PNG-embedded .ico (ICONDIR + one ICONDIRENTRY per size + the PNGs).
$out = New-Object System.IO.MemoryStream
$bw = New-Object System.IO.BinaryWriter($out)
$bw.Write([UInt16]0); $bw.Write([UInt16]1); $bw.Write([UInt16]$sizes.Count)   # ICONDIR
$offset = 6 + (16 * $sizes.Count)
for ($i = 0; $i -lt $sizes.Count; $i++) {
    $sz = $sizes[$i]; $data = $pngs[$i]
    $dim = if ($sz -ge 256) { 0 } else { $sz }     # 0 means 256 in the ICO spec
    $bw.Write([Byte]$dim); $bw.Write([Byte]$dim); $bw.Write([Byte]0); $bw.Write([Byte]0)
    $bw.Write([UInt16]1); $bw.Write([UInt16]32)
    $bw.Write([UInt32]$data.Length); $bw.Write([UInt32]$offset)
    $offset += $data.Length
}
foreach ($data in $pngs) { $bw.Write($data) }
$bw.Flush()

$target = Join-Path (Split-Path -Parent $PSScriptRoot) 'jarvis.ico'
[System.IO.File]::WriteAllBytes($target, $out.ToArray())
Write-Host "wrote $target ($($out.Length) bytes)"
