@echo off
cd /d "%~dp0\.."
py src\agent_backend.py --mode command --prompt %*
pause
