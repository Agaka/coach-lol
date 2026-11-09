"""Estimativa de tropas perdidas. A Live API não mostra as waves nem a posição de ninguém:
o máximo possível sai do calendário de waves (config.yaml), e as perdas são divididas pelo
que dá pra saber pela API: morto (isDead), base (compra + caminho de volta) e o resto na rota.
O creepScore só atualiza de 10 em 10, então tudo aqui tem margem de ±10."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from .logger import _mmss
from .rules import BAIXA, Advice
from .state import Change, GameState, Player

LANES = ("TOP", "MIDDLE", "BOTTOM")
STEP = 10   # o creepScore da Live API sobe de 10 em 10


@dataclass
class FarmEstimate:
    t: float
    possible: float
    cs: int              # o que a API mostra
    cs_est: float        # meio do degrau de 10
    dead: float
    base: float
    lane: float

    @property
    def lost(self) -> float:
        return self.dead + self.base + self.lane


class _Track:
    """Linha do tempo de um jogador: intervalos morto e longe da rota (base/caminho)."""

    def __init__(self, recall_s: float, back_s: float):
        self.recall_s, self.back_s = recall_s, back_s
        self.dead: list[list[float]] = []
        self.away: list[tuple[float, float]] = []
        self.prev: Player | None = None

    def update(self, t: float, p: Player, purchased: bool, seen_late: bool = False) -> None:
        prev = self.prev
        if p.is_dead and (prev is None or not prev.is_dead):
            self.dead.append([t, float("inf")])
        if prev is not None and prev.is_dead and not p.is_dead:
            if self.dead:
                self.dead[-1][1] = t
            self.away.append((t, t + self.back_s))          # renasceu: caminho da fonte até a rota
        if purchased and not p.is_dead:
            if seen_late:   # inimigo: a API mostra o item quando ele já está voltando, então a ausência ficou pra trás
                self.away.append((t - self.back_s - self.recall_s, t))
            else:
                self.away.append((t - self.recall_s, t + self.back_s))
        self.prev = p

    def status(self, x: float) -> str:
        if any(a <= x < b for a, b in self.dead):
            return "morto"
        if any(a <= x < b for a, b in self.away):
            return "base"
        return "rota"


class FarmTracker:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.fc: dict = {}
        self.role = ""
        self.first_wave: float | None = None
        self.me: _Track | None = None
        self.opp: _Track | None = None
        self.opp_key: str | None = None
        self.curve: list[tuple[FarmEstimate, FarmEstimate | None, int | None]] = []
        self.next_min = 1
        self.spoken: set[int] = set()
        self.stepped = True   # vira False se algum creepScore não for múltiplo de 10

    # ---------------- calendário de waves ----------------
    def _setup(self, s: GameState) -> None:
        fc = dict(self.cfg.get("farm", {}))
        fc.update((self.cfg.get("farm_por_modo") or {}).get(s.game_mode, {}) or {})
        self.fc = fc
        self.role = s.role
        lane = s.role if s.role in LANES else "MIDDLE"
        back = float((fc.get("volta_lane_s") or {}).get(lane, 30))
        self.me = _Track(float(fc.get("recall_s", 8)), back)
        self.opp = _Track(float(fc.get("recall_s", 8)), back)
        self.arrive = float((fc.get("chegada_s") or {}).get(lane, 30))

    def _cannon(self, n: int, spawn: float) -> int:
        every = 3
        for start, k in self.fc.get("canhao") or [[0, 3]]:
            if spawn >= float(start):
                every = int(k)
        return 1 if every > 0 and n % every == 0 else 0

    def waves(self, t: float):
        """(início da janela de farm, tropas) de cada wave que já chegou na rota."""
        first = self.first_wave if self.first_wave is not None else float(self.fc.get("primeira_wave_s", 30))
        step = float(self.fc.get("intervalo_wave_s", 30))
        per = int(self.fc.get("tropas_por_wave", 6))
        n = 1
        while True:
            spawn = first + (n - 1) * step
            arrive = spawn + self.arrive
            if arrive > t:
                return
            yield arrive, per + self._cannon(n, spawn)
            n += 1

    def estimate(self, t: float, cs: int, track: _Track) -> FarmEstimate:
        kill = float(self.fc.get("tempo_pra_matar_s", 25))
        possible = 0.0
        lost = Counter()
        for arrive, minions in self.waves(t):
            end = min(t, arrive + kill)
            secs = int(end - arrive)
            if secs <= 0:
                continue
            possible += minions * (end - arrive) / kill
            per_s = minions / kill
            for i in range(secs):
                st = track.status(arrive + i + 0.5)
                if st != "rota":
                    lost[st] += per_s
        cs_est = cs + (STEP - 1) / 2 if self.stepped and possible >= STEP else float(cs)
        total = max(0.0, possible - cs_est)
        away = lost["morto"] + lost["base"]
        if away > total:   # farmou outra wave/selva enquanto isso: limita ao que realmente faltou
            k = total / away if away else 0.0
            return FarmEstimate(t, possible, cs, cs_est, lost["morto"] * k, lost["base"] * k, 0.0)
        return FarmEstimate(t, possible, cs, cs_est, lost["morto"], lost["base"], total - away)

    # ---------------- ciclo ----------------
    def update(self, s: GameState, changes: list[Change], prev: GameState | None) -> list[Advice]:
        if self.me is None:
            self._setup(s)
        for c in changes:
            if c.kind == "event" and c.data.get("EventName") == "MinionsSpawning" and self.first_wave is None:
                self.first_wave = float(c.data.get("EventTime", 0))
        t = s.game_time
        if s.me.cs % STEP or (s.opponent is not None and s.opponent.cs % STEP):
            self.stepped = False
        self.me.update(t, s.me, any(c.kind == "purchase" for c in changes))
        o = s.opponent
        if o is not None:
            if o.key != self.opp_key:
                self.opp_key, self.opp.prev = o.key, None
            old = self.opp.prev
            bought = old is not None and bool(Counter(o.items) - Counter(old.items)) and o.item_value > old.item_value
            self.opp.update(t, o, bought, seen_late=True)
        if self.role not in LANES:
            return []
        out = []
        while t >= self.next_min * 60:
            m = self.next_min
            self.next_min += 1
            mine = self.estimate(m * 60, s.me.cs, self.me)
            theirs = self.estimate(m * 60, o.cs, self.opp) if o is not None else None
            self.curve.append((mine, theirs, o.cs if o is not None else None))
            if m in (self.fc.get("falar_em_min") or []) and m not in self.spoken:
                self.spoken.add(m)
                a = self._advice(mine, s)
                if a:
                    out.append(a)
        return out

    def _advice(self, e: FarmEstimate, s: GameState) -> Advice | None:
        if e.lost < float(self.fc.get("falar_se_perdeu", 8)):
            return None
        total = int(5 * round(e.lost / 5))
        parts = {"na rota": e.lane, "morto": e.dead, "na base": e.base}
        where = max(parts, key=parts.get)
        if parts[where] >= 0.8 * e.lost:
            txt = f"Perdeu umas {total} tropas, quase todas {where}"
        else:
            txt = f"Perdeu umas {total} tropas, {int(5 * round(parts[where] / 5)) or int(parts[where])} {where}"
        return Advice(txt, BAIXA, f"farm_{int(e.t // 60)}", cooldown=0, ttl=45, reason=self.line(e, s), created=s.game_time)

    # ---------------- texto ----------------
    def line(self, e: FarmEstimate, s: GameState | None = None, opp_cs: int | None = None) -> str:
        txt = (f"farm {e.cs} (≈{e.cs_est:.0f}) de {e.possible:.0f} possíveis | perdidas ≈{e.lost:.0f}: "
               f"rota {e.lane:.0f}, morto {e.dead:.0f}, base {e.base:.0f}")
        if opp_cs is not None:
            txt += f" | oponente {opp_cs}"
        return txt

    def summary(self, s: GameState | None) -> str:
        L = ["RESUMO DO FARM (estimativa: a API não mostra as waves; creepScore de 10 em 10, margem ±10)"]
        if self.role not in LANES:
            L.append(f"  Rota {self.role or '?'}: farm de selva/suporte não é estimado. CS final {s.me.cs if s else '?'}.")
            return "\n".join(L)
        if not self.curve:
            L.append("  partida curta demais.")
            return "\n".join(L)
        opp = s.opponent.champ_name if s and s.opponent else "oponente"
        L.append(f"  {'min':>3} {'possível':>8} {'você':>5} {opp[:10]:>10}   perdidas: {'rota':>4} {'morto':>5} {'base':>4}")
        for mine, theirs, ocs in self.curve:
            m = int(mine.t // 60)
            if m % 2 and m != int(self.curve[-1][0].t // 60):
                continue
            L.append(f"  {m:>3} {mine.possible:>8.0f} {mine.cs:>5} {('-' if ocs is None else ocs):>10}"
                     f"             {mine.lane:>4.0f} {mine.dead:>5.0f} {mine.base:>4.0f}")
        last, theirs, ocs = self.curve[-1]
        L.append(f"  Aos {_mmss(last.t)}: você perdeu ≈{last.lost:.0f} de {last.possible:.0f} "
                 f"({last.lost / max(1, last.possible):.0%}) — rota {last.lane:.0f}, morto {last.dead:.0f}, base {last.base:.0f}.")
        if theirs is not None:
            L.append(f"  {opp}: perdeu ≈{theirs.lost:.0f} — rota {theirs.lane:.0f}, morto {theirs.dead:.0f}, base {theirs.base:.0f}.")
        L.append("  'rota' = tropas que passaram enquanto você estava vivo e fora da base "
                 "(errou o último golpe, estava em outra rota, rotacionou).")
        return "\n".join(L)
