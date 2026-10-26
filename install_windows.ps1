param([switch]$CheckOnly,[switch]$ForcePython,[switch]$PythonProbeOnly)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding
$env:PYTHONIOENCODING = 'utf-8'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
function Suitable-Python($candidate) {
    if (-not $candidate -or -not (Test-Path -LiteralPath $candidate)) { return $false }
    if ($candidate -like '*\WindowsApps\*') { return $false }
    & $candidate -c "import sys,struct; raise SystemExit(0 if (3,11)<=sys.version_info[:2]<(3,15) and struct.calcsize('P')==8 else 1)" 2>$null
    return ($LASTEXITCODE -eq 0)
}
$python = $null
foreach ($minor in @('3.11','3.12','3.13','3.14')) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $found = & py "-$minor" -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and (Suitable-Python $found)) { $python=$found; break }
    }
}
if (-not $python) {
    foreach ($candidate in @("$env:LOCALAPPDATA\Programs\Python\Python314\python.exe", "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe", (Get-Command python -ErrorAction SilentlyContinue).Source)) {
        if (Suitable-Python $candidate) { $python=$candidate; break }
    }
}
if ($CheckOnly) { Write-Output ('Suitable Python found: ' + [bool]$python); exit 0 }
if ($ForcePython) { $python=$null }
if (-not $python) {
    Write-Host 'Python не найден. Скачиваю официальный установщик Python…'
    $manifest=Get-Content -LiteralPath 'python-downloads.json' -Raw | ConvertFrom-Json
    $arch=if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64' -or $env:PROCESSOR_ARCHITEW6432 -eq 'ARM64') {'windows-arm64'} else {'windows-x64'}
    if (-not [Environment]::Is64BitOperatingSystem) { throw 'Нужна 64-разрядная Windows 10 или новее.' }
    $item=$manifest.$arch
    if (-not $item.url.StartsWith('https://www.python.org/ftp/python/')) { throw 'Неподтверждённый адрес Python.' }
    $temp=Join-Path ([IO.Path]::GetTempPath()) ('dvk-python-'+[guid]::NewGuid().ToString('N')+'.exe')
    Invoke-WebRequest -UseBasicParsing -Uri $item.url -OutFile $temp -TimeoutSec 300
    if ((Get-FileHash -LiteralPath $temp -Algorithm SHA256).Hash.ToLowerInvariant() -ne $item.sha256) { throw 'Контрольная сумма Python не совпала. Установка остановлена.' }
    $signature=Get-AuthenticodeSignature -LiteralPath $temp
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'CN=Python Software Foundation') { throw 'Подпись Python не подтверждена.' }
    $target=Join-Path $env:LOCALAPPDATA 'Programs\Python\Python314'
    $arguments='/passive InstallAllUsers=0 PrependPath=0 Include_launcher=1 InstallLauncherAllUsers=0 Include_test=0 Include_doc=0 TargetDir="'+$target+'"'
    $process=Start-Process -FilePath $temp -ArgumentList $arguments -Wait -PassThru -WindowStyle Hidden
    if ($process.ExitCode -notin @(0,3010)) { throw 'Python не установился. Код: '+$process.ExitCode }
    $python=Join-Path $target 'python.exe'
    if (-not (Suitable-Python $python)) { throw 'Не удалось проверить установленный Python.' }
}
if ($PythonProbeOnly) {
    & $python -c "import sys,venv,ssl; print('Python bootstrap verified:',sys.version.split()[0])"
    exit $LASTEXITCODE
}
New-Item -ItemType Directory -Path 'data' -Force | Out-Null
[IO.File]::WriteAllText((Join-Path $PSScriptRoot 'data\python-path.txt'),$python,(New-Object System.Text.UTF8Encoding($false)))
Write-Host 'Python готов. Устанавливаю программу и открываю настройку…'
& $python install_manager.py
exit $LASTEXITCODE
