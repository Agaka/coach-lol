"""Fontes de snapshots: a Live Client Data API (ao vivo) e arquivos gravados (replay)."""
from __future__ import annotations

import gzip
import json
import ssl
import time
from pathlib import Path
from typing import Iterator

import httpx

from .config import ROOT

PEM_URL = "https://static.developer.riotgames.com/docs/lol/riotgames.pem"


def _ssl_context(pem: Path) -> ssl.SSLContext:
    """Valida a cadeia contra a raiz da Riot. O certificado do cliente não tem SAN
    pra 127.0.0.1, então a checagem de hostname é desligada (a conexão é local).
    O Python 3.13 liga VERIFY_X509_STRICT por padrão, que rejeita a raiz antiga da Riot."""
    if not pem.exists():
        r = httpx.get(PEM_URL, timeout=15)
        r.raise_for_status()
        pem.write_bytes(r.content)
    ctx = ssl.create_default_context(cafile=str(pem))
    ctx.check_hostname = False
    ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
    return ctx


class LiveClient:
    def __init__(self, cfg: dict):
        self.base = cfg["api"]["url"].rstrip("/")
        pem = ROOT / cfg["api"]["certificado"]
        self._verified = True
        try:
            verify: ssl.SSLContext | bool = _ssl_context(pem)
        except Exception as e:
            print(f"[api] não consegui preparar o certificado da Riot ({e}); seguindo sem verificação (só localhost)")
            verify, self._verified = False, False
        self.client = httpx.Client(verify=verify, timeout=1.5, trust_env=False)

    def fetch(self) -> dict | None:
        """Retorna allgamedata, ou None se não há partida (ou ela ainda está carregando)."""
        try:
            r = self.client.get(f"{self.base}/allgamedata")
        except httpx.ConnectError as e:
            if self._verified and "CERTIFICATE_VERIFY_FAILED" in str(e):
                # A Riot trocou o certificado? Não deixa o coach mudo por isso.
                print("[api] certificado do jogo não bateu com riotgames.pem; seguindo sem verificação (só localhost)")
                self.client = httpx.Client(verify=False, timeout=1.5, trust_env=False)
                self._verified = False
            return None
        except (httpx.TimeoutException, httpx.TransportError):
            return None
        if r.status_code != 200:
            return None
        try:
            data = r.json()
        except ValueError:
            return None
        if not is_valid_snapshot(data):
            return None
        return data


def is_valid_snapshot(data) -> bool:
    if not isinstance(data, dict):
        return False
    ap = data.get("activePlayer") or {}
    return "gameData" in data and bool(data.get("allPlayers")) and "championStats" in ap


# ---------------- replay ----------------

def read_snapshots(path: Path) -> list[dict]:
    """Lê um arquivo .jsonl ou .jsonl.gz com linhas {"t": tempo_real, "data": allgamedata}."""
    opener = gzip.open if str(path).endswith(".gz") else open
    out = []
    with opener(path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                break  # arquivo cortado (partida encerrada à força): para aqui
    return out


def replay(path: Path, speed: float = 1.0) -> Iterator[dict]:
    """Toca os snapshots respeitando o tempo de jogo entre eles, acelerado por `speed`."""
    snaps = read_snapshots(path)
    prev_gt = None
    for s in snaps:
        data = s["data"]
        gt = data["gameData"]["gameTime"]
        if prev_gt is not None and speed > 0:
            time.sleep(max(0.0, min(5.0, (gt - prev_gt) / speed)))
        prev_gt = gt
        yield data
