@echo off
REM Fase 4: baixa partidas ranqueadas de elo alto (Mestre+) pela API da Riot e gera as estatisticas.
REM Precisa de RIOT_API_KEY no chaves.env (chave de desenvolvimento em developer.riotgames.com, expira em 24 h).
REM Pode fechar no meio: na proxima vez ele continua de onde parou.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  python -m venv .venv
  .venv\Scripts\python -m pip install -q httpx pyyaml edge-tts anthropic
)
.venv\Scripts\python -m coach estatisticas %*
pause
