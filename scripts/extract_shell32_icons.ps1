# Extract every icon from C:\Windows\System32\shell32.dll to .ico files.
#
# One-shot helper: dumps all icons to %TEMP%\shell32_icons, then opens
# that folder in Explorer so you can browse thumbnails by eye and copy
# the one you want (e.g. tree.ico) into the repo's assets/ folder.
#
# Uses only Windows built-in APIs -- no downloads, no third-party tools.
# The P/Invoke block calls ExtractIconEx from shell32.dll; System.Drawing
# saves each icon handle out as a .ico file.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\extract_shell32_icons.ps1

Add-Type -AssemblyName System.Drawing

Add-Type -MemberDefinition @"
[DllImport("shell32.dll", CharSet=CharSet.Unicode)]
public static extern int ExtractIconEx(
    string sFile, int iIndex,
    out IntPtr piLargeVersion, out IntPtr piSmallVersion,
    int amountIcons);

[DllImport("user32.dll")]
public static extern bool DestroyIcon(IntPtr hIcon);
"@ -Name IconLib -Namespace Win32

$dll = "C:\Windows\System32\shell32.dll"
$outDir = Join-Path $env:TEMP "shell32_icons"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null

# Passing -1 as the index makes ExtractIconEx return the total icon count
# without actually extracting anything.
$large = [IntPtr]::Zero
$small = [IntPtr]::Zero
$count = [Win32.IconLib]::ExtractIconEx($dll, -1, [ref]$large, [ref]$small, 0)
Write-Host "shell32.dll contains $count icons.  Extracting to $outDir ..."

for ($i = 0; $i -lt $count; $i++) {
    $large = [IntPtr]::Zero
    $small = [IntPtr]::Zero
    [Win32.IconLib]::ExtractIconEx($dll, $i, [ref]$large, [ref]$small, 1) | Out-Null

    if ($large -ne [IntPtr]::Zero) {
        $icon = [System.Drawing.Icon]::FromHandle($large)
        $path = Join-Path $outDir ("icon_{0:000}.ico" -f $i)
        $stream = [System.IO.File]::Create($path)
        $icon.Save($stream)
        $stream.Close()
        $icon.Dispose()
        [Win32.IconLib]::DestroyIcon($large) | Out-Null
    }
    if ($small -ne [IntPtr]::Zero) {
        [Win32.IconLib]::DestroyIcon($small) | Out-Null
    }
}

Write-Host "Done.  Opening folder in Explorer."
explorer.exe $outDir
