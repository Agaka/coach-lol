"""Rotações: pra onde ir e quando, pela função do jogador. A regra de ouro que guia as falas é
"empurre a wave antes de sair da rota" (a wave empurrada segura o oponente e você não perde tropa).

O que dá pra saber pela Live API:
- tempos de objetivo (state.Objectives) -> lado do mapa: dragão = rio de baixo; vastilarvas, Arauto e Barão = rio de cima;
- torres e inibidores derrubados, pelo nome do evento: Turret_T<Order|Chaos>_L<rota>_P<camada>
  (L0 = bot, L1 = mid, L2 = topo; P3 externa, P2 interna, P1 do inibidor, P4/P5 do nexus;
  conferido numa partida real: a torre L0 do time ORDER caiu pra Nami e Viktor do bot);
- quem participou de cada objetivo (KillerName + Assisters) -> participação no resumo pós-jogo."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .logger import _mmss
from .rules import NORMAL, Advice
from .state import Change, GameState

LANE = {"0": "bot", "1": "mid", "2": "top"}
TIER = {"3": "externa", "2": "interna", "1": "do inibidor", "4": "do nexus", "5": "do nexus"}
ROLE_LANE = {"TOP": "top", "MIDDLE": "mid", "BOTTOM": "bot", "UTILITY": "bot"}
LANE_NAME = {"top": "topo", "mid": "mid", "bot": "bot"}
OBJ = {"DragonKill": ("dragão", "bot"), "HordeKill": ("vastilarvas", "top"), "HeraldKill": ("Arauto", "top"),
       "BaronKill": ("Barão", "top")}
STRUCT = re.compile(r"^(Turret|Inhib)_T(Order|Chaos)_L(\d)_P(\d)")


def parse_structure(name: str) -> tuple[str, str, str, str] | None:
    """'Turret_TChaos_L0_P3_...' -> ('Turret', 'CHAOS', 'bot', 'externa')."""
    m = STRUCT.match(str(name))
    if not m:
        return None
    kind, team, lane, tier = m.groups()
    return kind, team.upper(), LANE.get(lane, "?"), TIER.get(tier, "?")


def prep_phrase(key: str, role: str, mid_game: bool) -> str:
    """Complemento da fala de 1 minuto pro objetivo: o que fazer antes de ir, pela função."""
    side = "de baixo" if key == "dragon" else "de cima"
    obj = {"dragon": "dragão", "baron": "Barão", "herald": "Arauto", "grubs": "vastilarvas"}[key]
    pro, no = ("pras", "nas") if key == "grubs" else ("pro", "no")
    lane = ROLE_LANE.get(role)
    if role == "JUNGLE":
        return f"faça os campos {side} e vá {pro} {obj}"
    if mid_game:
        if lane and ((key == "dragon" and lane == "top") or (key != "dragon" and lane == "bot")):
            return f"empurre sua rota e venha {pro} {obj}"
        return f"empurrem as waves e agrupem {no} {obj}"
    if role == "MIDDLE":
        return f"empurre o mid e vá pro rio {side}"
    if lane == "bot":
        return "empurrem a wave e entrem no rio" if key == "dragon" else f"empurre forte enquanto brigam {no} {obj}"
    if lane == "top":
        return "empurre forte, vá só se brigarem" if key == "dragon" else f"empurre e ajude {no} {obj}"
    return "prepare a rota"


@dataclass
class ObjectiveRecord:
    t: float
    name: str
    ours: bool
    me_in: bool
    me_alive: bool


class RotationCoach:
    def __init__(self, cfg: dict):
        self.rc = cfg["rotacao"]
        self.down: list[tuple[float, str, str, str, str]] = []    # (t, tipo, time dono, rota, camada)
        self.objectives: list[ObjectiveRecord] = []
        self.my_structures = 0
        self.last_side_call = 0.0

    # ---------------- ciclo ----------------
    def update(self, s: GameState, changes: list[Change], resolve, last_spoken: dict) -> list[Advice]:
        out: list[Advice] = []
        for c in changes:
            if c.kind != "event":
                continue
            ev = c.data
            name = ev.get("EventName")
            names = [ev.get("KillerName")] + list(ev.get("Assisters") or [])
            me_in = any((p := resolve(n)) is not None and p.key == s.me.key for n in names if n)
            if name in ("TurretKilled", "InhibKilled"):
                st = parse_structure(ev.get("TurretKilled") or ev.get("InhibKilled") or "")
                if st:
                    t = float(ev.get("EventTime", s.game_time))
                    self.down.append((t,) + st)
                    self.my_structures += me_in
                    out += self._after_structure(s, st)
            elif name in OBJ:
                killer = ev.get("killer")
                if killer is not None:
                    t = float(ev.get("EventTime", s.game_time))
                    rec = ObjectiveRecord(t, OBJ[name][0], killer.team == s.me.team, me_in, not s.me.is_dead)
                    last = self.objectives[-1] if self.objectives else None
                    # as 3 vastilarvas saem em eventos separados, segundos de diferença: conta como um objetivo
                    if last and last.name == rec.name and last.ours == rec.ours and t - last.t < 45:
                        last.me_in = last.me_in or me_in
                    else:
                        self.objectives.append(rec)
        out += self._side_waves(s, last_spoken)
        return out

    # ---------------- falas ----------------
    def _after_structure(self, s: GameState, st) -> list[Advice]:
        kind, owner, lane, tier = st
        mine = ROLE_LANE.get(s.role)
        if kind != "Turret" or tier != "externa" or s.me.is_dead or s.game_time >= 20 * 60 or lane != mine:
            return []
        ours = owner != s.me.team          # derrubamos a torre deles
        if ours:
            txt = {"top": "Torre do topo caiu: vá pro rio ajudar o time",
                   "mid": "Torre do mid caiu: rode pros lados com o jungler",
                   "bot": "Torre do bot caiu: levem a pressão pro mid"}[lane]
            why = "torre externa derrubada: sua rota não segura mais o oponente, rodar ganha mais ouro e objetivo"
        else:
            txt = "Sua torre caiu: farme perto da interna"
            why = "torre externa perdida: a rota fica longa e exposta a gank"
        return [Advice(txt, NORMAL, "rot_tower", cooldown=60, ttl=20, created=s.game_time, reason=why,
                       check=lambda st_: not st_.me.is_dead)]

    def _side_waves(self, s: GameState, last_spoken: dict) -> list[Advice]:
        """Meio de jogo sem nada acontecendo: pegar a wave do lado do próximo objetivo."""
        t = s.game_time
        lim = float(self.rc.get("wave_lateral_intervalo_s", 150))
        if (not self.rc.get("wave_lateral", True) or t < 14 * 60 or t >= 30 * 60 or s.me.is_dead or s.at_base(40)
                or t - self.last_side_call < lim or s.enemies_dead or s.role in ("UTILITY", "JUNGLE")):
            return []
        if any(t - kt < 45 for k, kt in last_spoken.items() if k.startswith(("obj_", "sonnet_macro", "num_", "ace"))):
            return []
        o = s.objectives
        nxt = [(spawn, side, obj) for spawn, side, obj in (
            (o.next_dragon, "bot", "dragão"), (o.next_baron, "top", "Barão")) if spawn is not None]
        if not nxt:
            return []
        spawn, side, obj = min(nxt)
        if not 90 <= spawn - t <= 240:       # perto demais: a fala de objetivo já cuida; longe demais: cedo
            return []
        self.last_side_call = t
        txt = f"Pegue a wave do {LANE_NAME[side]} antes do {obj}"
        return [Advice(txt, NORMAL, "rot_side", cooldown=lim, ttl=20, created=t,
                       reason=f"{obj} nasce em {int(spawn - t)}s: wave empurrada no {LANE_NAME[side]} prende o inimigo longe",
                       check=lambda st_: not st_.me.is_dead)]

    # ---------------- contexto e resumo ----------------
    def structures(self, my_team: str) -> dict:
        lost = {"top": 0, "mid": 0, "bot": 0}
        taken = {"top": 0, "mid": 0, "bot": 0}
        for _, kind, owner, lane, _tier in self.down:
            if kind == "Turret" and lane in lost:
                (lost if owner == my_team else taken)[lane] += 1
        return {"torres_perdidas_por_rota": lost, "torres_derrubadas_por_rota": taken}

    def summary(self, s: GameState | None) -> str:
        L = ["RESUMO DE ROTAÇÃO (objetivos e torres)"]
        ours = [r for r in self.objectives if r.ours]
        theirs = [r for r in self.objectives if not r.ours]
        took = [r for r in ours if r.me_in]
        L.append(f"  Objetivos do seu time: {len(ours)}; você participou de {len(took)}"
                 + (f" ({', '.join(f'{r.name} {_mmss(r.t)}' for r in took)})" if took else "") + ".")
        missed = [r for r in ours if not r.me_in and r.me_alive]
        if missed:
            L.append("  Seu time fez sem você (você estava vivo): " + ", ".join(f"{r.name} {_mmss(r.t)}" for r in missed))
        alive_lost = [r for r in theirs if r.me_alive]
        if theirs:
            L.append(f"  Objetivos do inimigo: {len(theirs)}"
                     + (f"; você estava vivo em {len(alive_lost)}: " + ", ".join(f"{r.name} {_mmss(r.t)}" for r in alive_lost)
                        if alive_lost else "") + ".")
        if s is not None:
            st = self.structures(s.me.team)
            L.append(f"  Torres derrubadas pelo seu time: {sum(st['torres_derrubadas_por_rota'].values())} "
                     f"(topo {st['torres_derrubadas_por_rota']['top']}, mid {st['torres_derrubadas_por_rota']['mid']}, "
                     f"bot {st['torres_derrubadas_por_rota']['bot']}); perdidas: {sum(st['torres_perdidas_por_rota'].values())}. "
                     f"Você participou de {self.my_structures} estruturas.")
        L.append("  Dica: objetivo que o time faz sem você, com você vivo, normalmente é rotação atrasada; "
                 "empurre a wave 60 s antes e chegue junto.")
        return "\n".join(L)
