@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Python setup is required. See QUICKSTART.md.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" batch_transcripts.py --urls-file urls.txt
echo.
echo Captions and the result report are in the transcripts folder.
pause
