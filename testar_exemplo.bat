@echo off
REM Toca uma partida sintetica (Zed vs Ahri) com voz, a 6x, sem precisar do jogo.
cd /d "%~dp0"
.venv\Scripts\python -m coach exemplo -v 6
pause
