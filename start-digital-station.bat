@echo off
rem ======================================================================
rem  Digital Station (Tsifrovaya stantsiya) - launcher for any Windows 10/11 PC.
rem  Only Docker Desktop is required (the script checks it and offers to install).
rem  Usage: start-digital-station.bat [stop | status | update | uninstall]
rem  This cmd part is ASCII only and does not call chcp: with a UTF-8 file,
rem  chcp 65001 makes cmd misread the rest of the file and close the window.
rem ======================================================================
setlocal EnableDelayedExpansion
set "DS_BAT=%~f0"
set "DS_HERE=%~dp0"
set "DS_ARGS=%*"
set "DS_PS1=%TEMP%\digital-station-launcher.ps1"
set "DS_LOG=%TEMP%\digital-station-launch.log"
rem Everything below is ONE parenthesized block: cmd parses it before PowerShell
rem switches the console code page to UTF-8, so cmd never re-reads this file.
(
  where powershell >nul 2>nul || (echo PowerShell not found. & pause & exit /b 1)
  rem 1) extract the PowerShell part of this file into a temporary .ps1 (UTF-8 with BOM)
  powershell -NoProfile -ExecutionPolicy Bypass -Command "$c=[IO.File]::ReadAllText($env:DS_BAT,[Text.Encoding]::UTF8); $i=$c.LastIndexOf('#PS-' + 'START'); [IO.File]::WriteAllText($env:DS_PS1, $c.Substring($i), (New-Object Text.UTF8Encoding $true))"
  if not exist "!DS_PS1!" (
    echo Could not prepare the launcher in %TEMP%. Check antivirus or disk access.
    pause
    exit /b 1
  )
  rem 2) run it as a normal script file
  powershell -NoProfile -ExecutionPolicy Bypass -File "!DS_PS1!"
  set "RC=!ERRORLEVEL!"
  echo.
  if not "!RC!"=="0" echo Exit code !RC!. Log file: !DS_LOG!
  if not defined DS_NOPAUSE pause
  exit /b !RC!
)

#PS-START
# Continue: служебный вывод docker идёт в stderr и не должен прерывать сценарий; ошибки проверяются явно
$ErrorActionPreference = "Continue"
try { Start-Transcript -Path $env:DS_LOG -Force | Out-Null } catch { }
trap { Write-Host ""; Write-Host ("НЕПРЕДВИДЕННАЯ ОШИБКА: " + $_) -ForegroundColor Red; Write-Host "Журнал: $env:DS_LOG"; try { Stop-Transcript | Out-Null } catch { }; exit 2 }
[Console]::OutputEncoding = [Text.Encoding]::UTF8
try { [Console]::InputEncoding = [Text.Encoding]::UTF8 } catch { }
$Repo = "https://github.com/Romax01/Digtal-Nomads-Astana"
$ZipUrl = "$Repo/archive/refs/heads/main.zip"
$Action = (($env:DS_ARGS + "").Trim().ToLower() -split "\s+")[0]
if (-not $Action) { $Action = "start" }

function Say($t, $c = "Gray") { Write-Host $t -ForegroundColor $c }
function Step($t) { Write-Host ""; Write-Host "== $t" -ForegroundColor Cyan }
function Fail($t) { Write-Host ""; Write-Host "ОШИБКА: $t" -ForegroundColor Red; Write-Host "Журнал запуска: $env:DS_LOG" -ForegroundColor Gray; try { Stop-Transcript | Out-Null } catch { }; exit 1 }
function Ask($q, $def = "n") {
  $s = if ($def -eq "y") { "[Д/н]" } else { "[д/Н]" }
  $a = Read-Host "$q $s"
  if (-not $a) { $a = $def }
  return @("y", "д", "yes", "да", "l") -contains $a.Trim().ToLower()
}

Say "Цифровая станция — демонстрационная система поддержки решений (синтетические данные)." "White"

# ------------------------------------------------------------------ папка программы
$here = $env:DS_HERE.TrimEnd("\")
if (Test-Path (Join-Path $here "docker-compose.yml")) {
  $App = $here                                    # батник лежит в папке проекта
} else {
  $App = Join-Path $env:LOCALAPPDATA "DigitalStation\app"   # отдельная папка, ничего не трогаем вокруг
}
$Data = Split-Path $App -Parent
$EnvFile = Join-Path $App ".env"
# имя проекта Docker уникально для папки: несколько установок и другие программы не конфликтуют
$hash = [BitConverter]::ToString((New-Object Security.Cryptography.SHA1Managed).ComputeHash([Text.Encoding]::UTF8.GetBytes($App.ToLower()))).Replace("-", "").Substring(0, 8).ToLower()
$Project = "dstation-$hash"

# ------------------------------------------------------------------ Docker
function Test-Docker {
  try { $null = & docker version --format "{{.Server.Version}}" 2>$null; return $LASTEXITCODE -eq 0 } catch { return $false }
}
Step "Проверка Docker"
$dockerCli = Get-Command docker -ErrorAction SilentlyContinue
if (-not $dockerCli) {
  $dd = Join-Path $env:ProgramFiles "Docker\Docker\resources\bin\docker.exe"
  if (Test-Path $dd) { $env:Path = (Split-Path $dd) + ";" + $env:Path; $dockerCli = Get-Command docker -ErrorAction SilentlyContinue }
}
if (-not $dockerCli) {
  Say "Docker Desktop не установлен. Он нужен, чтобы запустить программу в изолированных контейнерах" "Yellow"
  Say "(база данных, сервер, интерфейс) — без установки Python, Node.js и библиотек в систему." "Yellow"
  if ((Get-Command winget -ErrorAction SilentlyContinue) -and (Ask "Установить Docker Desktop сейчас через winget (около 600 МБ, нужны права администратора)?" "y")) {
    & winget install -e --id Docker.DockerDesktop --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { Fail "Установка Docker Desktop не выполнена. Установите вручную: https://www.docker.com/products/docker-desktop/" }
    Say "Docker Desktop установлен. Перезагрузите компьютер (если попросит), запустите Docker Desktop, примите условия и снова запустите этот файл." "Green"
    exit 0
  }
  Fail "Установите Docker Desktop: https://www.docker.com/products/docker-desktop/ и снова запустите этот файл."
}
if (-not (Test-Docker)) {
  $exe = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
  if (Test-Path $exe) {
    Say "Docker Desktop не запущен — запускаю и жду готовности (до 3 минут)..." "Yellow"
    Start-Process $exe | Out-Null
    $t0 = Get-Date
    while (-not (Test-Docker)) {
      if (((Get-Date) - $t0).TotalSeconds -gt 180) { Fail "Docker не запустился за 3 минуты. Откройте Docker Desktop вручную, дождитесь «Engine running» и повторите." }
      Start-Sleep -Seconds 3; Write-Host "." -NoNewline
    }
    Write-Host ""
  } else { Fail "Служба Docker недоступна. Запустите Docker Desktop и повторите." }
}
& docker compose version *> $null
if ($LASTEXITCODE -ne 0) { Fail "Нет docker compose v2. Обновите Docker Desktop." }
Say ("Docker готов: " + (& docker version --format "{{.Server.Version}}")) "Green"

function Compose { & docker compose -p $Project --project-directory $App -f (Join-Path $App "docker-compose.yml") @args }

# ------------------------------------------------------------------ служебные команды
if ($Action -in @("stop", "status", "uninstall")) {
  if (-not (Test-Path (Join-Path $App "docker-compose.yml"))) { Fail "Программа не найдена в $App." }
  if ($Action -eq "stop") { Step "Остановка"; Compose stop; Say "Остановлено. Данные сохранены. Запуск — снова этот файл." "Green"; exit 0 }
  if ($Action -eq "status") { Compose ps; exit 0 }
  if (Ask "Удалить контейнеры И ВСЕ ДАННЫЕ программы (база, фото) из Docker?" "n") {
    Compose down -v --rmi local; Say "Удалено. Папку $App можно удалить вручную." "Green"
  }
  exit 0
}

# ------------------------------------------------------------------ загрузка программы
if (-not (Test-Path (Join-Path $App "docker-compose.yml")) -or $Action -eq "update") {
  Step "Загрузка программы с GitHub"
  New-Item -ItemType Directory -Force -Path $Data | Out-Null
  $zip = Join-Path $Data "app.zip"
  $tmp = Join-Path $Data "unpack"
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
  try { Invoke-WebRequest -Uri $ZipUrl -OutFile $zip -UseBasicParsing } catch { Fail "Не удалось скачать $ZipUrl. Проверьте интернет. ($($_.Exception.Message))" }
  if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
  Expand-Archive -Path $zip -DestinationPath $tmp -Force
  $src = Get-ChildItem $tmp -Directory | Select-Object -First 1
  if (-not (Test-Path (Join-Path $src.FullName "docker-compose.yml"))) { Fail "В архиве нет docker-compose.yml." }
  $keepEnv = if (Test-Path $EnvFile) { Get-Content $EnvFile -Raw } else { $null }
  if (Test-Path $App) { Remove-Item $App -Recurse -Force }
  Move-Item $src.FullName $App
  if ($keepEnv) { Set-Content -Path $EnvFile -Value $keepEnv -Encoding ASCII -NoNewline }
  Remove-Item $tmp -Recurse -Force; Remove-Item $zip -Force
  Say "Программа: $App" "Green"
}

# ------------------------------------------------------------------ настройки и порты без конфликтов
function Read-Env {
  $h = @{}
  if (Test-Path $EnvFile) { foreach ($l in Get-Content $EnvFile) { if ($l -match "^\s*([A-Z_]+)=(.*)$") { $h[$matches[1]] = $matches[2] } } }
  return $h
}
function New-Secret($n = 24) { -join ((48..57) + (65..90) + (97..122) | Get-Random -Count $n | ForEach-Object { [char]$_ }) }
function Port-Busy($p) {
  try { return [bool](Get-NetTCPConnection -State Listen -LocalPort $p -ErrorAction Stop) } catch { return $false }
}
$cfg = Read-Env
$running = @()
try { $running = @(Compose ps --status running --format "{{.Service}}" 2>$null) } catch { }
$ownRunning = $running.Count -gt 0

Step "Настройка"
if (-not $cfg.ContainsKey("SECRET_KEY")) {
  # уникальные пароли этой установки (хранятся только в $EnvFile)
  $cfg["SECRET_KEY"] = New-Secret 40; $cfg["POSTGRES_PASSWORD"] = New-Secret; $cfg["MQTT_INGEST_PASSWORD"] = New-Secret
  $cfg["MQTT_SIM_PASSWORD"] = New-Secret; $cfg["SIM_WORLD_TOKEN"] = New-Secret
}
$lan = $false
if ($cfg.ContainsKey("DS_LAN")) { $lan = $cfg["DS_LAN"] -eq "1" }
if ($env:DS_LAN) { $lan = $env:DS_LAN -eq "1" }            # без вопроса (автоматический запуск)
elseif (-not $cfg.ContainsKey("DS_LAN") -or $Action -eq "network") {
  $lan = Ask "Открыть доступ из локальной сети (телефоны работников, другие ПК в Wi-Fi)?" "y"
}
$cfg["DS_LAN"] = if ($lan) { "1" } else { "0" }

$want = [ordered]@{ FRONTEND_PORT = 8080; BACKEND_PORT = 8000; DB_PORT = 5432; MQTT_PORT = 1883 }
$taken = @{}
foreach ($k in @($want.Keys)) {
  $p = if ($cfg.ContainsKey("DS_$k")) { [int]$cfg["DS_$k"] } else { $want[$k] }
  $start = $p
  # порт занят другой программой (а не нашей уже запущенной установкой) — берём следующий свободный
  while ((-not $ownRunning -and (Port-Busy $p)) -or $taken.ContainsKey($p)) { $p++ }
  $taken[$p] = $true
  $cfg["DS_$k"] = "$p"
  if ($p -ne $start) { Say "  порт $start занят другой программой — использую $p" "Yellow" }
  elseif ($p -ne $want[$k]) { Say "  порт $p (сохранён при первом запуске: $($want[$k]) был занят)" "Gray" }
}
function Write-Env {
  $bind = if ($lan) { "" } else { "127.0.0.1:" }
  $cfg["FRONTEND_PORT"] = $bind + $cfg["DS_FRONTEND_PORT"]
  $cfg["MQTT_PORT"] = $bind + $cfg["DS_MQTT_PORT"]
  $cfg["BACKEND_PORT"] = $cfg["DS_BACKEND_PORT"]     # API и база — всегда только на этом ПК
  $cfg["DB_PORT"] = $cfg["DS_DB_PORT"]
  $lines = @("# Создано start-digital-station.bat. Секреты этой установки — не публикуйте.") + ($cfg.GetEnumerator() | Sort-Object Name | ForEach-Object { "$($_.Name)=$($_.Value)" })
  Set-Content -Path $EnvFile -Value $lines -Encoding ASCII
}
Write-Env

# ------------------------------------------------------------------ запуск
Step "Сборка и запуск контейнеров (первый раз — 5–15 минут, дальше — секунды)"
for ($try = 1; $try -le 4; $try++) {
  $log = @()
  Compose up -d --build 2>&1 | Tee-Object -Variable log | ForEach-Object { Write-Host $_ }
  if ($LASTEXITCODE -eq 0) { break }
  $text = ($log | Out-String)
  # порт зарезервирован Windows (Hyper-V) или занят — сдвигаем все порты установки и повторяем
  if ($try -lt 4 -and $text -match "port is already allocated|address already in use|access permissions|bind|ports are not available") {
    foreach ($k in @("DS_FRONTEND_PORT", "DS_BACKEND_PORT", "DS_DB_PORT", "DS_MQTT_PORT")) { $cfg[$k] = "" + ([int]$cfg[$k] + 10) }
    Say "Порт недоступен — повтор с портами $($cfg['DS_FRONTEND_PORT']), $($cfg['DS_MQTT_PORT']), $($cfg['DS_BACKEND_PORT']), $($cfg['DS_DB_PORT'])" "Yellow"
    Write-Env
    continue
  }
  Fail "docker compose не смог запустить программу. Подробности выше. Частые причины: мало места на диске или памяти, Docker Desktop остановлен."
}

Step "Ожидание готовности (база, миграции, демо-данные, план CP-SAT)"
$url = "http://127.0.0.1:$($cfg['DS_FRONTEND_PORT'])"
$t0 = Get-Date; $ok = $false
while (((Get-Date) - $t0).TotalSeconds -lt 900) {
  try { $r = Invoke-WebRequest "$url/api/v1/health" -UseBasicParsing -TimeoutSec 5; if ($r.StatusCode -eq 200) { $ok = $true; break } } catch { }
  Start-Sleep -Seconds 4; Write-Host "." -NoNewline
}
Write-Host ""
if (-not $ok) { Compose ps; Fail "Программа не ответила за 15 минут. Журнал: docker compose -p $Project logs backend" }

$ips = @()
try { $ips = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop | Where-Object { $_.IPAddress -notmatch "^(127|169\.254)\." -and $_.InterfaceAlias -notmatch "vEthernet|WSL|Docker|Loopback" -and $_.PrefixOrigin -ne "WellKnown" } | ForEach-Object IPAddress) } catch { }
Step "Готово"
Say "Основное приложение (ПК):   $url" "Green"
Say "Мобильное приложение:       $url/mobile" "Green"
if ($lan) { foreach ($ip in $ips) { Say "Из сети (телефоны, ПК):     http://${ip}:$($cfg['DS_FRONTEND_PORT'])   и   http://${ip}:$($cfg['DS_FRONTEND_PORT'])/mobile" "Green" } }
else { Say "Доступ из сети закрыт (только этот ПК)." "Gray" }
Say "Логины: station, duty, train, admin, viewer; работники: inspector, pto, fitter, senior. Пароль: demo123" "White"
Say "Остановить: start-digital-station.bat stop   ·   состояние: ... status   ·   обновить: ... update" "Gray"
Say "Данные и настройки: $App (.env) и тома Docker проекта $Project" "Gray"
if (-not $env:DS_NOPAUSE) { Start-Process $url | Out-Null }
exit 0
