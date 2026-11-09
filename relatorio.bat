@echo off
REM Relatorio de todas as partidas gravadas em logs\ (evolucao, habitos, tipos de morte).
REM Partida sem resultado (o jogo fechou antes do fim): crie resultado.txt na pasta dela com "vitoria" ou "derrota".
cd /d "%~dp0"
.venv\Scripts\python -m coach relatorio %*
start "" notepad "logs\relatorio.txt"
pause
