"""Regras determinísticas: transformam estado + mudanças em conselhos curtos."""
from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Callable

from .state import Change, GameState, lane_advantage

URGENTE, ALTA, NORMAL, BAIXA = 0, 1, 2, 3
PRIO_NOME = {URGENTE: "urgente", ALTA: "alta", NORMAL: "normal", BAIXA: "baixa"}
NUM = {2: "Dois", 3: "Três", 4: "Quatro", 5: "Cinco"}
LANE_PHASE_END = 14 * 60


@dataclass
class Advice:
    text: str
    priority: int
    key: str
    cooldown: float = 30.0   # não repetir a mesma chave antes disso (segundos de jogo)
    ttl: float = 15.0        # descartar se não foi falado em N segundos
    reason: str = ""
    created: float = 0.0
    check: Callable[[GameState], bool] | None = None   # ainda vale na hora de falar?


def _short_item(name: str, max_words: int = 3) -> str | None:
    return name if len(name.split()) <= max_words else None


class RuleEngine:
    def __init__(self, cfg: dict, dd):
        self.cfg = cfg
        self.dd = dd
        self.rc = cfg["regras"]
        self.prev: GameState | None = None
        self.last_lane_eval = 0.0
        self.last_lane_cat: str | None = None
        self.lane_cat_repeats = 0
        self.last_team_eval = 0.0
        self.deaths_to_opp = 0
        self.fed_announced: dict[str, int] = {}
        self.streak = 0                  # seus abates desde a última morte (a API não mostra a recompensa)
        self.bounty_spoken_at = 0        # sequência da última fala de recompensa
        self.last_lead_warn = -999.0
        self._tips = {}

    def _tip(self, kind: str, tips: list, s: GameState) -> str | None:
        """Próxima dica da lista, pulando as que pedem um feitiço que você não levou."""
        ok = [t if isinstance(t, str) else t.get("texto", "") for t in tips
              if isinstance(t, str) or t.get("requer_feitico") in s.me.spells]
        if not ok:
            return None
        if kind not in self._tips:
            self._tips[kind] = itertools.cycle(ok)
        return next(self._tips[kind])

    def evaluate(self, s: GameState, changes: list[Change]) -> list[Advice]:
        out: list[Advice] = []
        first = self.prev is None
        prev_t = self.prev.game_time if self.prev else s.game_time
        if first:
            out.append(self._welcome(s))
        out += self._objectives(s, prev_t)
        for c in changes:
            out += self._on_change(s, c)
        out += self._health(s)
        out += self._mana(s)
        out += self._gold(s)
        out += self._numbers(s)
        out += self._lane_eval(s)
        out += self._team_eval(s)
        out += self._fed_enemies(s)
        out += self._bounty(s)
        for a in out:
            a.created = s.game_time
        self.prev = s
        return out

    # ---------------- regras ----------------
    def _item_name(self, iid: int) -> str:
        it = self.dd.item(iid)
        return it["name"] if it else "item"

    def _welcome(self, s: GameState) -> Advice:
        if s.opponent:
            txt = f"Coach ligado. {s.me.champ_name} contra {s.opponent.champ_name}."
        else:
            txt = f"Coach ligado. Boa partida de {s.me.champ_name}."
        if not s.in_pool:
            txt += " Modo genérico."
        return Advice(txt, NORMAL, "welcome", cooldown=1e9, ttl=30, reason="início da sessão")

    def _objectives(self, s: GameState, prev_t: float) -> list[Advice]:
        out = []
        o = s.objectives
        t = s.game_time
        role = s.role
        mid_game = t >= LANE_PHASE_END
        my_team = s.me.team
        enemy_team = s.enemies[0].team if s.enemies else None
        objs = [
            ("dragon", "Dragão Ancião" if o.elder_next else "Dragão", o.next_dragon,
             role in ("JUNGLE", "BOTTOM", "UTILITY", "MIDDLE") or mid_game),
            ("baron", "Barão", o.next_baron, True),
            ("herald", "Arauto", o.next_herald, role in ("JUNGLE", "TOP", "MIDDLE")),
            ("grubs", "Vastilarvas", o.next_grubs, role in ("JUNGLE", "TOP", "MIDDLE")),
        ]
        for key, name, spawn, relevant in objs:
            if spawn is None:
                continue
            if key == "grubs" and o.next_herald is not None and spawn >= o.next_herald:
                continue
            for warn in self.cfg["objetivos"]["avisos_s"]:
                at = spawn - warn
                if prev_t < at <= t:
                    soul = key == "dragon" and not o.elder_next and (
                        o.soul_point(my_team) or (enemy_team and o.soul_point(enemy_team)))
                    if warn >= 60:
                        if not relevant:
                            continue
                        from .rotation import prep_phrase   # aqui pra não importar em círculo
                        txt = f"{name} em 1 minuto: {prep_phrase(key, role, mid_game)}"
                        prio = NORMAL
                    else:
                        if soul:
                            txt = "Ponto de alma em 30 segundos, agrupem"
                        elif key == "dragon" and o.elder_next:
                            txt = "Ancião em 30 segundos, agrupem todos"
                        else:
                            txt = f"{name} nasce em {warn} segundos"
                        prio = ALTA if relevant else NORMAL
                    out.append(Advice(txt, prio, f"obj_{key}_{warn}", cooldown=60, ttl=12,
                                      reason=f"{name} nasce em {spawn:.0f}s de jogo"))
            if prev_t < spawn <= t and relevant:
                out.append(Advice(f"{name} nasceu", BAIXA, f"obj_{key}_spawn", cooldown=60, ttl=10,
                                  reason="timer de objetivo"))
        return out

    def _on_change(self, s: GameState, c: Change) -> list[Advice]:
        out = []
        me, opp = s.me, s.opponent
        prof = s.profile
        lane = s.game_time < LANE_PHASE_END

        if c.kind == "level_up" and opp is not None and lane:
            lvl = c.data["level"]
            # "troque agora" / "procure o all-in" só com metade da vida ou mais
            # (Zed x Ekko: "Ult antes dele, procure o all-in" com 44% e morte 20 s depois)
            if c.player.key == me.key and s.hp_pct >= 0.5:
                # só nos níveis fortes do campeão: nível 2 do Gragas contra LeBlanc perdeu a troca
                # (ela subiu 3 s depois e o pico dela é justamente no 2)
                if lvl in (2, 3) and me.level > opp.level and lvl in prof.get("niveis_fortes", []):
                    out.append(Advice(f"Nível {lvl} primeiro, troque agora", ALTA, f"lvl{lvl}_first",
                                      cooldown=1e9, ttl=8, reason=f"você {me.level} x {opp.level} ele",
                                      check=lambda st: not st.me.is_dead and st.hp_pct >= 0.5))
                elif lvl == 6 and opp.level < 6:
                    txt = "Ult antes dele, procure o all-in" if int(prof.get("allin_nivel", 6)) <= 6 \
                        else "Nível 6 primeiro, pressione"
                    out.append(Advice(txt, ALTA, "lvl6_first", cooldown=1e9, ttl=10,
                                      reason=f"você 6 x {opp.level} ele",
                                      check=lambda st: not st.me.is_dead and st.hp_pct >= 0.5))
            elif c.player.key == opp.key:
                if lvl in (2, 3) and opp.level > me.level:
                    out.append(Advice(f"{opp.champ_name} tem nível {lvl}, cuidado", ALTA, f"opp_lvl{lvl}",
                                      cooldown=1e9, ttl=8, reason=f"ele {opp.level} x {me.level} você"))
                elif lvl == 6 and me.level < 6:
                    out.append(Advice(f"{opp.champ_name} tem ult, jogue seguro", ALTA, "opp_lvl6",
                                      cooldown=1e9, ttl=10, reason=f"ele 6 x {me.level} você"))

        elif c.kind == "level_up" and c.player.key == me.key and c.data["level"] == 6 and opp is None:
            out.append(Advice("Ultimate disponível", BAIXA, "lvl6", cooldown=1e9, ttl=10))

        elif c.kind == "item_completed":
            item = c.data["item"]
            if c.player.key == me.key:
                n = len([i for i in me.completed if not self.dd.is_boots(i)])
                name = self._item_name(item)
                if self.dd.is_boots(item):
                    return out
                if n == int(prof.get("itens_spike", 1)):
                    sn = _short_item(name)
                    txt = f"Spike com {sn}, hora de pressionar" if sn else "Spike de item, hora de pressionar"
                    out.append(Advice(txt, ALTA, f"my_spike_{n}", cooldown=1e9, ttl=20,
                                      reason=f"{name} = item {n}"))
            elif opp is not None and c.player.key == opp.key and not self.dd.is_boots(item):
                name = self._item_name(item)
                sn = _short_item(name, 2)
                txt = f"{opp.champ_name} fechou {sn}, respeite" if sn else f"{opp.champ_name} fechou item, respeite a troca"
                out.append(Advice(txt, ALTA, f"opp_item_{item}", cooldown=1e9, ttl=20, reason=f"{name}"))

        elif c.kind == "my_death":
            killer = c.data.get("killer")
            if killer is not None and opp is not None and killer.key == opp.key:
                self.deaths_to_opp += 1
                if self.deaths_to_opp >= 2 and lane:
                    out.append(Advice(f"{opp.champ_name} te matou de novo, jogue seguro", NORMAL,
                                      "died_opp", cooldown=120, ttl=25,
                                      reason=f"{self.deaths_to_opp} mortes pro oponente"))

        elif c.kind == "my_respawn":
            adv, _ = lane_advantage(s)
            if lane and opp is not None and adv <= self.rc["vantagem_seguro"]:
                out.append(Advice("Jogue seguro até fechar seu item", NORMAL, "respawn_safe",
                                  cooldown=120, ttl=15, reason=f"vantagem {adv}"))

        elif c.kind == "event":
            out += self._on_event(s, c.data)
        return out

    def _on_event(self, s: GameState, ev: dict) -> list[Advice]:
        out = []
        name = ev.get("EventName")
        me, opp = s.me, s.opponent
        mine = ev.get("killer_team") == me.team
        if name == "ChampionKill":
            victim = ev.get("victim")
            killer = ev.get("killer")
            if killer is not None and killer.key == me.key:
                self.streak += 1
            if victim is not None and victim.key == me.key:
                self.streak, self.bounty_spoken_at = 0, 0
            if opp is not None and victim is not None and victim.key == opp.key and not me.is_dead:
                if s.role == "JUNGLE":
                    txt = "Jungler inimigo morto, invada a selva"
                elif s.game_time < LANE_PHASE_END:
                    txt = "Oponente morto, empurre e pegue placas"
                else:
                    txt = "Oponente morto, empurre a torre"
                out.append(Advice(txt, ALTA, "opp_dead", cooldown=20, ttl=10, reason="abate no oponente de lane",
                                  check=lambda st: st.opponent is not None and st.opponent.is_dead
                                  and not st.me.is_dead))
        elif name == "Ace":
            if str(ev.get("AcingTeam", "")) == me.team:
                out.append(Advice("Ace! Façam objetivo ou torre", URGENTE, "ace", cooldown=30, ttl=10))
        elif name == "BaronKill":
            if mine:
                out.append(Advice("Barão nosso, empurrem juntos agora", ALTA, "baron_ours", cooldown=60, ttl=15))
            elif ev.get("killer_team"):
                out.append(Advice("Barão inimigo, defendam perto das torres", ALTA, "baron_theirs",
                                  cooldown=60, ttl=15))
        elif name == "DragonKill":
            o = s.objectives
            team = ev.get("killer_team")
            if team and o.dragons[team] == o.soul_at:
                txt = "Alma do dragão é nossa" if mine else "Alma inimiga, cuidado nas lutas"
                out.append(Advice(txt, NORMAL, "soul", cooldown=1e9, ttl=15))
        elif name == "InhibKilled":
            inhib = str(ev.get("InhibKilled", ""))
            # nome real: Inhib_TOrder_L1_P1_... / Inhib_TChaos_L0_P1_... (time dono do inibidor)
            my_tag = "TOrder" if me.team == "ORDER" else "TChaos"
            if f"_{my_tag}_" in inhib:
                out.append(Advice("Inibidor perdido, defendam a base", NORMAL, "inhib_lost", cooldown=60, ttl=15))
        return out

    def _mana(self, s: GameState) -> list[Advice]:
        """Pouca mana na fase de rota: sem mana não troca nem farma com habilidade."""
        st = s.stats or {}
        if (str(st.get("resourceType", "")).upper() != "MANA" or s.me.is_dead or s.at_base()
                or s.game_time < 90 or s.game_time >= LANE_PHASE_END or self.prev is None):
            return []
        cur = float(st.get("resourceValue", 0)) / max(1.0, float(st.get("resourceMax", 1)))
        prev = self.prev.stats or {}
        old = float(prev.get("resourceValue", 0)) / max(1.0, float(prev.get("resourceMax", 1)))
        low = float(self.rc.get("mana_baixa_pct", 0.2))
        if cur < low <= old:
            txt = "Pouca mana: volte pra base" if s.gold >= 700 else "Pouca mana: farme com ataques, sem trocar"
            return [Advice(txt, NORMAL, "low_mana", cooldown=120, ttl=10, reason=f"mana {cur:.0%}, ouro {s.gold:.0f}",
                           check=lambda st_: not st_.me.is_dead and not st_.at_base())]
        return []

    def _health(self, s: GameState) -> list[Advice]:
        if s.me.is_dead or s.at_base() or s.game_time < 60 or self.prev is None:
            return []
        p, prev_p = s.hp_pct, self.prev.hp_pct
        crit, low = self.rc["vida_critica_pct"], self.rc["vida_baixa_pct"]
        alive_low = lambda st: not st.me.is_dead and not st.at_base() and st.hp_pct < low + 0.1
        if p < crit <= prev_p:
            return [Advice("Vida crítica, recue agora", URGENTE, "hp_crit", cooldown=20, ttl=4,
                           reason=f"vida {p:.0%}", check=alive_low)]
        if p < low <= prev_p:
            if s.gold >= 1000:
                return [Advice("Vida baixa e ouro, volte pra base", ALTA, "hp_low", cooldown=40, ttl=6,
                               reason=f"vida {p:.0%}, ouro {s.gold:.0f}", check=alive_low)]
            return [Advice("Vida baixa, jogue seguro", ALTA, "hp_low", cooldown=40, ttl=6, reason=f"vida {p:.0%}",
                           check=alive_low)]
        return []

    def _gold(self, s: GameState) -> list[Advice]:
        if s.me.is_dead or s.at_base(30) or s.game_time < 90:
            return []
        th = self.rc["ouro_guardado"]
        still = lambda st: not st.me.is_dead and not st.at_base(30) and st.gold >= th
        if s.gold >= th * 2:
            return [Advice("Muito ouro parado, volte já", ALTA, "gold_high", cooldown=90, ttl=30,
                           reason=f"ouro {s.gold:.0f}", check=still)]
        if s.gold >= th:
            return [Advice("Ouro sobrando, volte pra comprar", NORMAL, "gold", cooldown=120, ttl=30,
                           reason=f"ouro {s.gold:.0f}", check=still)]
        return []

    def _numbers(self, s: GameState) -> list[Advice]:
        if self.prev is None or s.me.is_dead:
            return []
        out = []
        ed, ped = s.enemies_dead, self.prev.enemies_dead
        if ed >= 2 and ed > ped:
            obj = _alive_objective(s)
            tail = f"vão no {obj}" if obj else "empurrem uma torre"
            out.append(Advice(f"{NUM.get(ed, ed)} inimigos mortos, {tail}", URGENTE, "num_adv",
                              cooldown=25, ttl=8, reason=f"{ed} inimigos mortos",
                              check=lambda st: st.enemies_dead >= 2 and not st.me.is_dead))
        ad, pad = s.allies_dead, self.prev.allies_dead
        if ad >= 3 and ad > pad:
            out.append(Advice("Time morto, recue e jogue seguro", ALTA, "num_disadv",
                              cooldown=30, ttl=10, reason=f"{ad} aliados mortos"))
        return out

    def _lane_eval(self, s: GameState) -> list[Advice]:
        if (s.opponent is None or s.me.is_dead or s.at_base() or s.game_time >= LANE_PHASE_END
                or s.game_time < 120 or s.game_time - self.last_lane_eval < self.rc["intervalo_avaliacao_lane_s"]):
            return []
        self.last_lane_eval = s.game_time
        score, reasons = lane_advantage(s)
        # com menos de metade da vida não manda pressionar, mesmo à frente em nível/itens
        # (Mel x Diana: "pressione" com 39% de vida e morte 17 s depois)
        if score >= self.rc["vantagem_pressionar"] and s.hp_pct >= 0.5:
            cat = "pressionar"
        elif score <= self.rc["vantagem_seguro"]:
            cat = "seguro"
        else:
            cat = "neutro"
        same = cat == self.last_lane_cat
        self.last_lane_cat = cat
        self.lane_cat_repeats = self.lane_cat_repeats + 1 if same else 0
        if cat == "neutro" or (same and self.lane_cat_repeats % 2 == 1):
            return []
        why = f"pontuação {score}: " + (", ".join(reasons) or "equilíbrio")
        if cat == "pressionar":
            tip = self._tip("v", s.profile.get("dicas_vantagem", []), s) if same else None
            txt = tip or "Vantagem na lane, pressione"
        else:
            tip = self._tip("d", s.profile.get("dicas_desvantagem", []), s) if same else None
            txt = tip or "Desvantagem, jogue seguro e farme"
        return [Advice(txt, NORMAL, f"lane_{cat}", cooldown=60, ttl=20, reason=why,
                       check=lambda st: not st.me.is_dead and not st.at_base())]

    def _team_eval(self, s: GameState) -> list[Advice]:
        if s.game_time < LANE_PHASE_END or s.game_time - self.last_team_eval < 200 or not s.enemies:
            return []
        self.last_team_eval = s.game_time
        diff = sum(p.item_value for p in s.allies) - sum(p.item_value for p in s.enemies)
        kills = sum(p.kills for p in s.allies) - sum(p.kills for p in s.enemies)
        why = f"itens do time {diff:+d} de ouro, abates {kills:+d}"
        if diff >= 2500:
            return [Advice("Time à frente, forcem objetivos juntos", NORMAL, "team_ahead",
                           cooldown=180, ttl=30, reason=why)]
        if diff <= -2500:
            return [Advice("Time atrás, defendam e farmem", NORMAL, "team_behind",
                           cooldown=180, ttl=30, reason=why)]
        return []

    def _bounty(self, s: GameState) -> list[Advice]:
        """Você vale recompensa: abates seguidos ou muito à frente em itens. Zed na frente atrai o time
        inimigo inteiro; morrer sozinho devolve a vantagem (partida de 2026-11-04: 6 mortes pego por vários)."""
        if s.me.is_dead or s.at_base(30):
            return []
        t = s.game_time
        need = int(self.rc.get("recompensa_abates", 3))
        if self.streak >= need and (self.bounty_spoken_at == 0 or self.streak >= self.bounty_spoken_at + 2):
            self.bounty_spoken_at = self.streak
            return [Advice("Você vale recompensa: não ande sozinho", ALTA, "bounty", cooldown=60, ttl=20,
                           reason=f"{self.streak} abates sem morrer", check=lambda st: not st.me.is_dead)]
        if not s.enemies or t < 18 * 60 or s.enemies_dead:
            return []
        lead = s.me.item_value - sum(e.item_value for e in s.enemies) / len(s.enemies)
        if (lead >= float(self.rc.get("recompensa_ouro", 2500))
                and t - self.last_lead_warn >= float(self.rc.get("recompensa_intervalo_s", 240))):
            self.last_lead_warn = t
            return [Advice("Muito na frente: lute junto com o time", NORMAL, "bounty_lead", cooldown=120, ttl=20,
                           reason=f"seus itens {s.me.item_value} x média inimiga {lead:+.0f}",
                           check=lambda st: not st.me.is_dead and not st.enemies_dead)]
        return []

    def _fed_enemies(self, s: GameState) -> list[Advice]:
        out = []
        for e in s.enemies:
            lead = e.kills - e.deaths
            last = self.fed_announced.get(e.key, 3)
            if lead >= 4 and lead >= last + 1:
                self.fed_announced[e.key] = lead + 1
                out.append(Advice(f"{e.champ_name} está forte, evite lutar sozinho", NORMAL,
                                  f"fed_{e.key}", cooldown=180, ttl=20,
                                  reason=f"{e.kills}/{e.deaths}/{e.assists}"))
        return out


# ---------------- utilidades ----------------
def _alive_objective(s: GameState) -> str | None:
    o, t = s.objectives, s.game_time
    if t >= o.next_baron:
        return "Barão"
    if t >= o.next_dragon:
        return "Ancião" if o.elder_next else "dragão"
    if o.next_herald is not None and t >= o.next_herald:
        return "Arauto"
    return None
