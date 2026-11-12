@echo off
setlocal
cd /d "%~dp0"
title KlipLokal - Local Video Downloader

where yt-dlp >nul 2>&1
if errorlevel 1 (
    echo yt-dlp belum ditemukan. Tutup jendela ini dan buka ulang setelah instalasi.
    echo Perintah instalasi: winget install --source winget --id yt-dlp.yt-dlp
    goto failed
)

if not exist ".venv\Scripts\python.exe" (
    py -3 -m venv .venv
    if errorlevel 1 (
        python -m venv .venv
        if errorlevel 1 goto failed
    )
)
".venv\Scripts\python.exe" -c "import playwright.sync_api" >nul 2>&1
if errorlevel 1 (
    echo Menyiapkan pemindai browser. Perlu internet saat setup pertama.
    ".venv\Scripts\python.exe" -m pip install "playwright>=1.51,<2"
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" app.py
if errorlevel 1 goto failed
exit /b 0

:failed
echo.
echo KlipLokal belum bisa dibuka. Periksa pesan di atas.
pause
exit /b 1
