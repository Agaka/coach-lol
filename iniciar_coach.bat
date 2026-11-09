@echo off
REM Abre o coach ao vivo. Pode abrir antes ou depois de entrar na partida.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Instalando dependencias pela primeira vez...
  python -m venv .venv
  .venv\Scripts\python -m pip install -q httpx pyyaml edge-tts anthropic
)
.venv\Scripts\python -c "import anthropic" 2>nul || .venv\Scripts\python -m pip install -q anthropic
.venv\Scripts\python -m coach %*
pause
