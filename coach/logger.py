"""Grava snapshots e conselhos de cada partida em logs/<data>_<campeão>/."""
from __future__ import annotations

import gzip
import json
import time
from datetime import datetime
from pathlib import Path

from .config import ROOT
from .rules import PRIO_NOME, Advice


class SessionLogger:
    def __init__(self, cfg: dict, champ: str, tag: str = ""):
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.dir = ROOT / cfg["log"]["pasta"] / f"{stamp}_{champ}{tag}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.save_snaps = cfg["log"].get("salvar_snapshots", True)
        self._snaps = gzip.open(self.dir / "snapshots.jsonl.gz", "at", encoding="utf-8") if self.save_snaps else None
        self._advice = open(self.dir / "conselhos.jsonl", "a", encoding="utf-8")
        self._text = open(self.dir / "partida.log", "a", encoding="utf-8")
        self._n = 0

    def snapshot(self, raw: dict) -> None:
        if self._snaps:
            self._snaps.write(json.dumps({"t": time.time(), "data": raw}, ensure_ascii=False) + "\n")
            self._n += 1
            if self._n % 30 == 0:
                self._snaps.flush()

    def advice(self, a: Advice, status: str, game_time: float) -> None:
        rec = {
            "horario": datetime.now().isoformat(timespec="seconds"),
            "tempo_jogo": round(game_time, 1),
            "criado_em": round(a.created, 1),
            "texto": a.text,
            "prioridade": PRIO_NOME[a.priority],
            "chave": a.key,
            "status": status,
            "motivo": a.reason,
        }
        self._advice.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._advice.flush()
        if status == "falado":
            line = f"[{_mmss(game_time)}] 🔊 {a.text}" + (f"   ({a.reason})" if a.reason else "")
            print(line)
            self.text(line)

    def text(self, line: str) -> None:
        self._text.write(line + "\n")
        self._text.flush()

    def close(self) -> None:
        for f in (self._snaps, self._advice, self._text):
            if f:
                f.close()


def _mmss(t: float) -> str:
    t = max(0, int(t))
    return f"{t // 60:02d}:{t % 60:02d}"
