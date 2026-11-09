"""Voz: edge-tts (pt-BR neural) com cache de mp3 e tocador nativo do Windows (MCI).
Fila com prioridade e intervalo mínimo entre falas (urgentes furam o intervalo)."""
from __future__ import annotations

import asyncio
import ctypes
import hashlib
import queue
import subprocess
import threading
import time
from pathlib import Path

from .config import ROOT
from .rules import URGENTE, Advice

CACHE = ROOT / "data" / "tts_cache"


def _play_mp3(path: Path) -> None:
    winmm = ctypes.windll.winmm
    alias = f"coach{threading.get_ident()}{int(time.time() * 1000) % 100000}"
    buf = ctypes.create_unicode_buffer(256)

    def mci(cmd: str) -> None:
        err = winmm.mciSendStringW(cmd, buf, 255, 0)
        if err:
            msg = ctypes.create_unicode_buffer(256)
            winmm.mciGetErrorStringW(err, msg, 255)
            raise RuntimeError(f"MCI: {msg.value}")

    mci(f'open "{path}" type mpegvideo alias {alias}')
    try:
        mci(f"play {alias} wait")
    finally:
        winmm.mciSendStringW(f"close {alias}", None, 0, 0)


def _sapi_say(text: str) -> None:
    """Plano B sem internet: voz do Windows (usa uma voz pt-BR se houver)."""
    ps = (
        "Add-Type -AssemblyName System.Speech;"
        "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$v=$s.GetInstalledVoices()|?{$_.VoiceInfo.Culture.Name -eq 'pt-BR'}|select -First 1;"
        "if($v){$s.SelectVoice($v.VoiceInfo.Name)};"
        f"$s.Speak('{text.replace(chr(39), '')}')"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=20)


class Voice:
    def __init__(self, cfg: dict, enabled: bool = True):
        vc = cfg["voz"]
        self.voice = vc["voz"]
        self.rate = vc["velocidade"]
        self.volume = vc["volume"]
        self.enabled = enabled and vc.get("ativada", True)
        self._q: queue.Queue[str | None] = queue.Queue()
        self._busy = threading.Event()
        self._edge_ok = True
        CACHE.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    @property
    def busy(self) -> bool:
        return self._busy.is_set() or not self._q.empty()

    def say(self, text: str) -> None:
        if self.enabled:
            self._busy.set()
            self._q.put(text)

    def wait_idle(self, timeout: float = 30) -> None:
        end = time.time() + timeout
        while self.busy and time.time() < end:
            time.sleep(0.1)

    def close(self) -> None:
        self._q.put(None)

    def _mp3_for(self, text: str) -> Path:
        h = hashlib.sha1(f"{self.voice}|{self.rate}|{self.volume}|{text}".encode()).hexdigest()[:16]
        path = CACHE / f"{h}.mp3"
        if not path.exists():
            import edge_tts
            tmp = path.with_suffix(".tmp")
            asyncio.run(edge_tts.Communicate(text, self.voice, rate=self.rate, volume=self.volume).save(str(tmp)))
            tmp.replace(path)
        return path

    def _worker(self) -> None:
        while True:
            text = self._q.get()
            if text is None:
                return
            self._busy.set()
            try:
                if self._edge_ok:
                    try:
                        _play_mp3(self._mp3_for(text))
                    except Exception as e:
                        print(f"[voz] edge-tts falhou ({e}); usando voz do Windows")
                        self._edge_ok = False
                        _sapi_say(text)
                else:
                    _sapi_say(text)
            except Exception as e:
                print(f"[voz] erro: {e}")
            finally:
                if self._q.empty():
                    self._busy.clear()


class SpeechQueue:
    """Decide o que falar e quando. Tempo medido em segundos de jogo."""

    def __init__(self, voice: Voice, min_gap: float, on_result):
        self.voice = voice
        self.min_gap = min_gap
        self.on_result = on_result          # callback(advice, status, game_time)
        self.pending: list[Advice] = []
        self.last_spoken_t = -1e9
        self.last_by_key: dict[str, float] = {}

    def push(self, a: Advice, now: float) -> None:
        last = self.last_by_key.get(a.key)
        if last is not None and now - last < a.cooldown:
            return
        # mesma chave já na fila: fica a mais nova
        for old in [p for p in self.pending if p.key == a.key]:
            self.pending.remove(old)
        self.last_by_key[a.key] = now
        self.pending.append(a)

    def tick(self, now: float, state=None) -> None:
        for a in list(self.pending):
            if now - a.created > a.ttl:
                self.pending.remove(a)
                self.on_result(a, "expirado", now)
            elif a.check and state is not None and not a.check(state):
                # a situação mudou antes da fala (ex.: morreu, já comprou)
                self.pending.remove(a)
                self.on_result(a, "invalido", now)
        if not self.pending or self.voice.busy:
            return
        self.pending.sort(key=lambda p: (p.priority, p.created))
        best = self.pending[0]
        if best.priority != URGENTE and now - self.last_spoken_t < self.min_gap:
            return
        self.pending.pop(0)
        self.last_spoken_t = now
        self.voice.say(best.text)
        self.on_result(best, "falado", now)

    def flush_report(self, now: float) -> None:
        for a in self.pending:
            self.on_result(a, "nao_falado_fim", now)
        self.pending.clear()
