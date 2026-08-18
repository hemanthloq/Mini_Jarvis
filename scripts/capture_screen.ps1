# Capture the whole (virtual) screen to a PNG, scaled down for a vision model.
# Used by the look_at_screen tool. Dependency-free (System.Drawing).
#   powershell -File scripts\capture_screen.ps1 -Out C:\path\shot.png
param([Parameter(Mandatory=$true)][string]$Out, [int]$MaxW = 1400)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms

$vs = [System.Windows.Forms.SystemInformation]::VirtualScreen
$full = New-Object System.Drawing.Bitmap $vs.Width, $vs.Height
$g = [System.Drawing.Graphics]::FromImage($full)
$g.CopyFromScreen($vs.Location, [System.Drawing.Point]::Empty, $vs.Size)
$g.Dispose()

if ($vs.Width -gt $MaxW) {
    $scale = $MaxW / $vs.Width
    $w = [int]($vs.Width * $scale); $h = [int]($vs.Height * $scale)
    $small = New-Object System.Drawing.Bitmap $w, $h
    $sg = [System.Drawing.Graphics]::FromImage($small)
    $sg.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $sg.DrawImage($full, 0, 0, $w, $h)
    $sg.Dispose(); $full.Dispose()
    $small.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png); $small.Dispose()
} else {
    $full.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png); $full.Dispose()
}
Write-Host "saved $Out"
