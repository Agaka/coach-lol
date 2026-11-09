"""Uso:
  python -m coach                         ao vivo (espera a partida sozinho)
  python -m coach replay ARQUIVO [-v 4]   toca snapshots gravados (.jsonl ou .jsonl.gz)
  python -m coach exemplo [-v 6]          gera uma partida sintética e toca em replay
  python -m coach testar-voz              fala uma frase de teste
  python -m coach estatisticas [--partidas 500]   baixa partidas de elo alto (RIOT_API_KEY)
  python -m coach relatorio [--sem-sonnet]        relatório de todas as partidas em logs/
Opções: --sem-voz (só texto), --sem-jev, --sem-sonnet, -v/--velocidade (replay)
"""
import argparse
import sys
from pathlib import Path

from .app import run_live, run_replay
from .config import ROOT


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    ap = argparse.ArgumentParser(prog="coach", description="Coach de LoL por voz")
    ap.add_argument("modo", nargs="?", default="ao-vivo", choices=["ao-vivo", "replay", "exemplo", "testar-voz", "estatisticas", "relatorio"])
    ap.add_argument("arquivo", nargs="?")
    ap.add_argument("-v", "--velocidade", type=float, default=4.0)
    ap.add_argument("--sem-voz", action="store_true")
    ap.add_argument("--sem-jev", action="store_true")
    ap.add_argument("--sem-sonnet", action="store_true")
    ap.add_argument("--partidas", type=int)
    a = ap.parse_args()

    if a.modo == "ao-vivo":
        run_live(a.sem_voz, a.sem_jev, a.sem_sonnet)
    elif a.modo == "replay":
        if not a.arquivo:
            ap.error("informe o arquivo de snapshots")
        run_replay(Path(a.arquivo), a.velocidade, a.sem_voz, a.sem_jev, a.sem_sonnet)
    elif a.modo == "exemplo":
        from .sample import write_sample
        path = write_sample(ROOT / "replays" / "exemplo_zed_vs_ahri.jsonl.gz")
        run_replay(path, a.velocidade, a.sem_voz, a.sem_jev, a.sem_sonnet)
    elif a.modo == "estatisticas":
        from .config import load_config
        from .elo import download
        download(load_config(), a.partidas)
    elif a.modo == "relatorio":
        from .report import run
        run(use_sonnet=not a.sem_sonnet)
    elif a.modo == "testar-voz":
        from .config import load_config
        from .voice import Voice
        v = Voice(load_config())
        v.say("Coach ligado. Dragão nasce em 30 segundos.")
        v.wait_idle(20)
        print("Se você ouviu a frase, a voz está ok.")


if __name__ == "__main__":
    main()
