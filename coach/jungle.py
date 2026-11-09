"""Rastreio do caçador inimigo por dedução. A Live API não dá posição de ninguém; o que ela entrega e
serve de pista:
- abates, torres e inibidores com o nome de quem participou -> onde ele estava naquele segundo
  (lado do mapa = rota dos outros participantes);
- objetivos do time inimigo: dragão = rio de baixo; vastilarvas, arauto e barão = rio de cima;
- item novo NÃO serve pra saber se ele foi pra base: a API só mostra o item do inimigo com atraso
  (numa partida real, 18 a 22 s depois dele renascer e comprar, quando ele já voltava pra rota);
- nível exato: nível 3 ou 4 cedo = terminou campos e pode gankar.
O farm (creepScore) sobe de 10 em 10 e quase não ajuda; fica só no log."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from .logger import _mmss
from .rules import ALTA, NORMAL, Advice
from .state import Change, GameState, Player

SIDE = {"TOP": "top", "MIDDLE": "mid", "BOTTOM": "bot", "UTILITY": "bot"}
WHERE = {"top": "no topo", "mid": "no mid", "bot": "no bot"}
OBJ_SIDE = {"DragonKill": "bot", "HordeKill": "top", "HeraldKill": "top", "BaronKill": "top"}
OBJ_NAME = {"DragonKill": "dragão", "HordeKill": "vastilarvas", "HeraldKill": "Arauto", "BaronKill": "Barão"}


@dataclass
class Sighting:
    t: float
    side: str          # top | mid | bot
    how: str
    river: bool = False   # objetivo no rio (dragão, vastilarvas, Arauto, Barão): colado no mid


class JungleTracker:
    def __init__(self, cfg: dict, dd):
        self.jc = cfg["cacador"]
        self.dd = dd
        self.jg_key: str | None = None
        self.prev: dict[str, Player] = {}
        self.items_seen: list[tuple[float, str]] = []    # (quando a API mostrou item novo, campeão)
        self.seen: list[Sighting] = []
        self.warned_level: set[int] = set()
        self.missing_warned_for: float | None = None

    # ---------------- quem é ----------------
    @staticmethod
    def find_jungler(enemies: list[Player]) -> Player | None:
        return next((e for e in enemies if e.position == "JUNGLE"), None) or next((e for e in enemies if e.has_smite), None)

    @property
    def last(self) -> Sighting | None:
        return self.seen[-1] if self.seen else None

    # ---------------- ciclo ----------------
    def update(self, s: GameState, changes: list[Change], resolve) -> list[Advice]:
        jg = self.find_jungler(s.enemies)
        if jg is None:
            return []
        self.jg_key = jg.key
        out: list[Advice] = []
        for c in changes:
            if c.kind == "event":
                sg = self._from_event(c.data, jg, s, resolve)
                if sg:
                    out += self._sighted(sg, s, jg)
        # item novo: só registro. A API mostra o item do inimigo atrasado, então não dá pra dizer
        # "voltou pra base" na hora certa (falar isso quando ele já está de volta engana o jogador)
        for p in [jg] + ([s.opponent] if s.opponent and s.opponent.key != jg.key else []):
            old = self.prev.get(p.key)
            if old is not None and self._bought(Counter(p.items) - Counter(old.items)):
                self.items_seen.append((s.game_time, p.champ_name))
            self.prev[p.key] = p
        out += self._levels(s, jg)
        out += self._missing(s, jg)
        return out

    def _bought(self, new: Counter) -> bool:
        """Item que só se consegue na loja. Botas da runa e biscoitos aparecem fora da base (purchasable=False)."""
        for iid in new:
            it = self.dd.item(iid)
            if it and (it.get("gold") or {}).get("purchasable", False):
                return True
        return False

    def _from_event(self, ev: dict, jg: Player, s: GameState, resolve) -> Sighting | None:
        name = ev.get("EventName")
        t = float(ev.get("EventTime", s.game_time))
        names = [ev.get("KillerName")] + list(ev.get("Assisters") or [])
        involved = [resolve(n) for n in names if n]
        jg_in = any(p is not None and p.key == jg.key for p in involved)
        if name in OBJ_SIDE:
            killer = ev.get("killer")
            if killer is not None and killer.team == jg.team:   # objetivo inimigo: o jungler quase sempre está lá
                return Sighting(t, OBJ_SIDE[name], f"time inimigo fez {OBJ_NAME[name]}", river=True)
            return None
        if name not in ("ChampionKill", "TurretKilled", "InhibKilled"):
            return None
        victim = ev.get("victim")
        if victim is not None and victim.key == jg.key:
            return None                                  # morreu: a regra de abate já avisa
        if not jg_in:
            return None
        others = [p for p in involved + [victim] if p is not None and p.key != jg.key]
        sides = Counter(SIDE[p.position] for p in others if p.position in SIDE)
        side = sides.most_common(1)[0][0] if sides else None
        if side is None:
            return None
        what = {"ChampionKill": f"abate em {victim.champ_name if victim else '?'}", "TurretKilled": "torre",
                "InhibKilled": "inibidor"}[name]
        return Sighting(t, side, what)

    # ---------------- falas ----------------
    def _sighted(self, sg: Sighting, s: GameState, jg: Player) -> list[Advice]:
        self.seen.append(sg)
        self.missing_warned_for = None
        if s.game_time - sg.t > 10 or s.me.is_dead or not self.jc.get("falar", True):
            return []
        if s.game_time >= float(self.jc.get("ate_min", 20)) * 60:
            return []
        me = "selva" if s.role == "JUNGLE" else SIDE.get(s.role)
        txt = None
        if me == "selva":
            if sg.side in ("top", "bot"):
                other = "de baixo" if sg.side == "top" else "de cima"
                txt = f"Jungler deles {WHERE[sg.side]}: jogue no lado {other}"
        elif me:
            if sg.side == me:
                txt = f"Jungler inimigo {WHERE[sg.side]}, perto de você"
            elif me in ("top", "bot") and sg.side in ("top", "bot"):
                txt = f"Jungler inimigo {WHERE[sg.side]}: pode jogar pra frente"
            elif me == "mid" and sg.river:
                # rio de cima/baixo fica a segundos do mid (Yasuo x Master Yi: vastilarvas 8:51, gank 9:12)
                rio = "de cima" if sg.side == "top" else "de baixo"
                txt = f"Jungler inimigo no rio {rio}: cuidado no mid"
            elif me == "mid":
                other = "de baixo" if sg.side == "top" else "de cima"
                txt = f"Jungler inimigo {WHERE[sg.side]}: o lado {other} está livre"
            elif sg.side == "mid":
                txt = "Jungler inimigo no mid, atenção ao rio"
        if not txt:
            return []
        stamp = sg.t
        prio = ALTA if sg.side == me or (me == "mid" and sg.river) else NORMAL
        key = jg.key

        def still_valid(st) -> bool:
            # informação mais nova chegou, ou ele morreu na mesma luta: não fala
            alive = not any(e.key == key and e.is_dead for e in st.enemies)
            return not st.me.is_dead and alive and self.last is not None and self.last.t == stamp

        # chave por lado: notícia nova de outro lado não fica presa no intervalo da anterior
        return [Advice(txt, prio, f"jg_{sg.side}", cooldown=float(self.jc.get("intervalo_s", 40)), ttl=15,
                       created=s.game_time, reason=f"{jg.champ_name}: {sg.how} {WHERE[sg.side]} às {_mmss(sg.t)}",
                       check=still_valid)]

    def _levels(self, s: GameState, jg: Player) -> list[Advice]:
        """Primeira volta na selva: nível 3 = 3 campos (gank possível), nível 4 = selva toda."""
        if s.role == "JUNGLE" or s.me.is_dead:
            return []
        lv, t = jg.level, s.game_time
        if lv >= 3 and 3 not in self.warned_level and t < 4 * 60:
            self.warned_level.add(3)
            return [Advice("Jungler inimigo nível 3: cuidado com gank", NORMAL, "jg_lvl", cooldown=60, ttl=10,
                           created=t, reason=f"{jg.champ_name} nível 3 às {_mmss(t)}")]
        if lv >= 4 and 4 not in self.warned_level and t < 5 * 60:
            self.warned_level.add(4)
            return [Advice("Jungler inimigo fechou a selva: risco de gank", NORMAL, "jg_lvl", cooldown=60, ttl=10,
                           created=t, reason=f"{jg.champ_name} nível 4 às {_mmss(t)}")]
        return []

    def _missing(self, s: GameState, jg: Player) -> list[Advice]:
        """Visto longe de você e sumido faz tempo: pode estar vindo."""
        last = self.last
        lim = float(self.jc.get("sumido_s", 75))
        me = SIDE.get(s.role)
        if (last is None or me is None or s.me.is_dead or jg.is_dead or last.side == me
                or self.missing_warned_for == last.t or s.game_time >= 14 * 60 or s.game_time - last.t < lim):
            return []
        self.missing_warned_for = last.t
        return [Advice("Jungler inimigo sumido faz tempo: jogue com cuidado", NORMAL, "jg_missing", cooldown=90, ttl=10,
                       created=s.game_time, reason=f"visto por último {WHERE[last.side]} às {_mmss(last.t)}",
                       check=lambda st: not st.me.is_dead)]

    # ---------------- contexto (Sonnet / log) ----------------
    def describe(self, s: GameState) -> dict | None:
        jg = self.find_jungler(s.enemies)
        if jg is None:
            return None
        d = {"campeao": jg.champ_name, "nivel": jg.level, "farm": jg.cs, "morto": jg.is_dead}
        if self.last:
            d["visto_por_ultimo"] = {"onde": self.last.side, "como": self.last.how, "ha_segundos": int(s.game_time - self.last.t)}
        return d
