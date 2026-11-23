@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================
echo   BeamWeft 更新器
echo ============================================
echo.
echo 启动器会显示正在使用的实验资料库位置。
echo.

rem 注意:if/括号块内的 echo 文本一律使用全角括号（），半角 ) 会提前闭合块。

rem ---- 先确认能更新,再动正在运行的程序:更新不了就不该杀掉用户正在用的 BeamWeft ----
where git >nul 2>nul
if errorlevel 1 goto :no_git
if not exist ".git" goto :no_git

echo 正在关闭正在运行的 BeamWeft...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$repo = '%CD%'; foreach ($p in 8000,5173) { $conns = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique; foreach ($procId in $conns) { $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue; if ($null -eq $proc) { continue }; $cmdline = (Get-CimInstance Win32_Process -Filter ('ProcessId=' + $procId) -ErrorAction SilentlyContinue).CommandLine; if ($cmdline -and ($cmdline -like '*uvicorn app.main*' -or $cmdline -like '*vite*' -or $cmdline -like ('*' + $repo + '*'))) { Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue } else { Write-Output ('[警告] 端口 ' + $p + ' 被其他程序占用 (' + $proc.ProcessName + '),未动它。') } } }"

rem ---- 整块用括号包裹:cmd 会一次性读入整个块再执行。
rem git pull 会改写本文件自身,若逐行读盘执行,改写后的内容会错位乱跑。 ----
(
  echo 正在从 GitHub 拉取最新代码...
  git pull --ff-only
  if errorlevel 1 (
    echo.
    echo [错误] 自动更新失败（常见原因:网络不通,或本地文件被改动过）。
    echo 请截图此窗口内容找维护者;或让维护者发一份新版压缩包,
    echo 解压到一个新文件夹后直接双击其中的 Launch-BeamWeft.bat
    echo （实验资料库的位置可以在启动窗口中查看）。
    echo.
    pause
    exit /b 1
  )
  echo.
  echo 更新完成！正在为你启动新版本...
  start "" "%~dp0Launch-BeamWeft.bat"
  exit /b 0
)

:no_git
echo.
echo [提示] 这台电脑没有安装 Git,或此文件夹不是通过 Git 下载的,无法自动更新。
echo 更新方法:让维护者发一份新版压缩包,解压到一个新文件夹,
echo 然后双击其中的 Launch-BeamWeft.bat 即可。
echo 重新启动时会继续使用已有实验资料库。具体位置可在启动窗口中查看。
echo.
pause
exit /b 0
