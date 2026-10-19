# Забрать свежую копию базы с сервера (Windows, без Git Bash). Запуск — двойным щелчком по «Забрать бэкап.cmd».
# 1) на сервере делается бэкап прямо сейчас; 2) скачивается в data\backups; 3) проверяется; 4) хранятся 30 последних.
# Continue, не Stop: в Windows PowerShell 5.1 вывод ssh в stderr иначе обрывает скрипт без понятного сообщения
$ErrorActionPreference = 'Continue'
$Project = Split-Path -Parent $PSScriptRoot
$HostFile = Join-Path $PSScriptRoot 'host.local'
$Key = Join-Path $env:USERPROFILE '.ssh\basket_assist_vps'
$Dest = Join-Path $Project 'data\backups'
$Keep = 30

function Fail($msg) { Write-Host "`nОШИБКА: $msg" -ForegroundColor Red; exit 1 }

if (-not (Test-Path $HostFile)) { Fail "нет файла $HostFile (одна строка: root@<IP сервера>)" }
$Server = (Get-Content $HostFile -TotalCount 1).Trim()
if (-not (Test-Path $Key)) { Fail "нет SSH-ключа $Key" }
$ssh = @('-i', $Key, '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=20')

Write-Host "Сервер: $Server"
Write-Host 'Делаю бэкап на сервере...'
$out = & ssh @ssh $Server 'systemctl start basket-assist-backup.service && ls -1t /opt/basket-assist/backups/basket_assist_*.db.gz | head -1' 2>&1 |
    ForEach-Object { "$_" }
if ($LASTEXITCODE -ne 0) { Fail "сервер не ответил или бэкап не удался:`n$($out -join "`n")" }
$remote = "$($out | Select-Object -Last 1)".Trim()
if ($remote -notmatch '^/opt/basket-assist/backups/basket_assist_.*\.db\.gz$') { Fail "не понял ответ сервера: $remote" }

New-Item -ItemType Directory -Force -Path $Dest | Out-Null
$name = Split-Path -Leaf $remote
$local = Join-Path $Dest $name
Write-Host "Скачиваю $name ..."
& scp -i $Key -o BatchMode=yes -q "${Server}:$remote" $local
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $local)) { Fail 'не удалось скачать файл' }

# проверка: файл — настоящий gzip с базой SQLite внутри
try {
    $fs = [IO.File]::OpenRead($local)
    $gz = New-Object IO.Compression.GZipStream($fs, [IO.Compression.CompressionMode]::Decompress)
    $buf = New-Object byte[] 16
    $null = $gz.Read($buf, 0, 16)
    $gz.Close(); $fs.Close()
    if ([Text.Encoding]::ASCII.GetString($buf, 0, 15) -ne 'SQLite format 3') { Fail 'скачанный файл — не база SQLite' }
} catch { Fail "скачанный файл повреждён: $_" }

$old = Get-ChildItem $Dest -Filter 'basket_assist_*.db.gz' | Sort-Object LastWriteTime -Descending | Select-Object -Skip $Keep
$old | Remove-Item -Force
$sizeKb = [math]::Round((Get-Item $local).Length / 1KB)
$count = (Get-ChildItem $Dest -Filter 'basket_assist_*.db.gz').Count
Write-Host "`nГотово: $local ($sizeKb КБ)" -ForegroundColor Green
Write-Host "Копий в папке: $count (храню $Keep последних)"
