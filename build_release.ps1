$ErrorActionPreference = 'Stop'

$root = $PSScriptRoot
$version = '2.0.1'

$files = @(
    'main.py',
    'bot.py',
    'database.py',
    'updater.py'
)

$outDir = Join-Path $root 'data\updates\release_v2.0.1'
$stage = Join-Path $outDir 'stage'
$zip = Join-Path $outDir 'telegram_ai_sender_v2.0.1.zip'
$shaFile = $zip + '.sha256'

try {
    Write-Host 'Checking source files...'

    foreach ($name in $files) {
        $path = Join-Path $root $name

        if (!(Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Missing required file: $name"
        }
    }

    New-Item -ItemType Directory -Path $outDir -Force |
        Out-Null

    if (Test-Path -LiteralPath $stage) {
        Remove-Item -LiteralPath $stage -Recurse -Force
    }

    New-Item -ItemType Directory -Path $stage |
        Out-Null

    foreach ($name in $files) {
        Copy-Item `
            -LiteralPath (Join-Path $root $name) `
            -Destination (Join-Path $stage $name)
    }

    Set-Content `
        -LiteralPath (Join-Path $stage 'version.txt') `
        -Value $version `
        -Encoding ascii

    if (Test-Path -LiteralPath $zip) {
        Remove-Item -LiteralPath $zip -Force
    }

    if (Test-Path -LiteralPath $shaFile) {
        Remove-Item -LiteralPath $shaFile -Force
    }

    Write-Host 'Creating ZIP archive...'

    Compress-Archive `
        -Path (Join-Path $stage '*') `
        -DestinationPath $zip `
        -CompressionLevel Optimal

    Write-Host 'Validating ZIP contents...'

    Add-Type -AssemblyName System.IO.Compression.FileSystem

    $archive = [System.IO.Compression.ZipFile]::OpenRead($zip)

    try {
        $actualNames = @(
            $archive.Entries |
                ForEach-Object { $_.FullName } |
                Sort-Object
        )

        $expectedNames = @(
            'bot.py',
            'database.py',
            'main.py',
            'updater.py',
            'version.txt'
        ) | Sort-Object

        $difference = Compare-Object $expectedNames $actualNames

        if ($difference) {
            throw "Unexpected archive contents: $($actualNames -join ', ')"
        }

        $versionEntry = $archive.GetEntry('version.txt')

        if ($null -eq $versionEntry) {
            throw 'version.txt is missing from the archive.'
        }

        $reader = New-Object System.IO.StreamReader(
            $versionEntry.Open(),
            [System.Text.Encoding]::ASCII
        )

        try {
            $archiveVersion = $reader.ReadToEnd().Trim()
        }
        finally {
            $reader.Dispose()
        }

        if ($archiveVersion -ne $version) {
            throw "Unexpected version inside archive: $archiveVersion"
        }
    }
    finally {
        $archive.Dispose()
    }

    $hash = (
        Get-FileHash -LiteralPath $zip -Algorithm SHA256
    ).Hash.ToLowerInvariant()

    Set-Content `
        -LiteralPath $shaFile `
        -Value ($hash + '  ' + [IO.Path]::GetFileName($zip)) `
        -Encoding ascii

    $savedHash = (
        Get-Content -LiteralPath $shaFile
    ).Split(' ')[0]

    if ($savedHash -ne $hash) {
        throw 'SHA256 verification failed.'
    }

    Write-Host ''
    Write-Host 'PACKAGE CREATED AND VERIFIED'
    Write-Host "Version: $version"
    Write-Host "ZIP: $zip"
    Write-Host "SHA256: $hash"
    Write-Host 'Archive contents:'

    foreach ($name in $actualNames) {
        Write-Host "  $name"
    }

    Write-Host ''
    Write-Host 'No release was published.'
    Write-Host 'The working version.txt was not changed.'
}
catch {
    Write-Host ''
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}