"""Transforma snapshots crus da Live API em estado do jogo e mudanças detectadas."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from .config import champion_profile
from .ddragon import DataDragon

ROLES = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")


@dataclass
class Player:
    key: str
    names: set[str]
    champ_id: str | None
    champ_name: str
    team: str
    position: str
    level: int
    items: list[int]
    item_value: int
    completed: list[int]
    kills: int
    deaths: int
    assists: int
    cs: int
    ward_score: float
    is_dead: bool
    respawn: float
    is_bot: bool
    has_smite: bool
    spells: frozenset[str] = frozenset()   # ids crus dos feitiços, ex.: SummonerFlash, SummonerDot (Incendiar)
    spell_names: tuple[str, ...] = ()      # nomes como o jogo mostra, ex.: ("Flash", "Golpear")


@dataclass
class Change:
    kind: str                 # level_up, item_completed, my_death, my_respawn, purchase, event
    player: Player | None = None
    data: dict = field(default_factory=dict)


@dataclass
class Objectives:
    next_dragon: float
    next_baron: float
    next_herald: float | None
    next_grubs: float | None
    dragons: Counter = field(default_factory=Counter)  # time -> quantidade
    elder_next: bool = False
    baron_buff_team: str | None = None
    baron_buff_until: float = 0.0
    soul_at: int = 4                # dragões pra alma (2 no Swiftplay)

    def soul_point(self, team: str) -> bool:
        return self.dragons[team] == self.soul_at - 1 and not self.elder_next


@dataclass
class GameState:
    game_time: float
    me: Player
    allies: list[Player]
    enemies: list[Player]
    opponent: Player | None
    role: str
    stats: dict
    gold: float
    hp: float
    max_hp: float
    abilities: dict[str, int]
    objectives: Objectives
    profile: dict
    in_pool: bool
    last_purchase_t: float
    game_mode: str

    @property
    def hp_pct(self) -> float:
        return self.hp / self.max_hp if self.max_hp else 1.0

    @property
    def enemies_dead(self) -> int:
        return sum(p.is_dead for p in self.enemies)

    @property
    def allies_dead(self) -> int:
        return sum(p.is_dead for p in self.allies)

    def at_base(self, window: float = 20.0) -> bool:
        return self.game_time - self.last_purchase_t < window


def _spell_ids(p: dict) -> frozenset[str]:
    """'GeneratedTip_SummonerSpell_SummonerDot_DisplayName' -> 'SummonerDot'."""
    out = set()
    for v in (p.get("summonerSpells") or {}).values():
        m = re.search(r"SummonerSpell_(\w+?)_DisplayName", str(v.get("rawDisplayName", "")))
        if m:
            out.add(m.group(1))
    return frozenset(out)


def _spell_raw(p: dict) -> str:
    ss = p.get("summonerSpells") or {}
    return " ".join(str(v.get("rawDisplayName", "")) + " " + str(v.get("displayName", "")) for v in ss.values())


class StateTracker:
    def __init__(self, dd: DataDragon, champs: dict, cfg: dict):
        self.dd = dd
        self.champs = champs
        self.cfg = cfg
        self.prev: GameState | None = None
        self.last_event_id = -1
        self.opponent_key: str | None = None
        self.last_purchase_t = -999.0
        self.oc: dict = {}
        self.obj: Objectives | None = None   # criado no 1º snapshot, quando o modo de jogo é conhecido
        self.grubs_killed = 0
        self.kill_log: list[dict] = []   # abates com jogadores resolvidos

    # ---------- jogadores ----------
    def _player(self, p: dict) -> Player:
        cid = self.dd.champ_id(p.get("rawChampionName")) or self.dd.champ_id(p.get("championName"))
        items = []
        value = 0
        for it in p.get("items") or []:
            iid = int(it.get("itemID", 0))
            items.append(iid)
            info = self.dd.item(iid)
            if info and "Consumable" not in info.get("tags", []):
                value += self.dd.item_cost(iid)
        names = {str(p.get(k)).lower() for k in ("summonerName", "riotId", "riotIdGameName") if p.get(k)}
        if p.get("championName"):
            names.add(f"{p['championName']} bot".lower())
        sc = p.get("scores") or {}
        key = str(p.get("riotId") or p.get("summonerName") or p.get("championName"))
        return Player(
            key=key,
            names=names,
            champ_id=cid,
            champ_name=self.dd.champ_name(cid) if cid else str(p.get("championName", "?")),
            team=str(p.get("team", "")),
            position=str(p.get("position") or "").upper(),
            level=int(p.get("level", 1)),
            items=items,
            item_value=value,
            completed=[i for i in items if self.dd.is_completed(i)],
            kills=int(sc.get("kills", 0)),
            deaths=int(sc.get("deaths", 0)),
            assists=int(sc.get("assists", 0)),
            cs=int(sc.get("creepScore", 0)),
            ward_score=float(sc.get("wardScore", 0)),
            is_dead=bool(p.get("isDead", False)),
            respawn=float(p.get("respawnTimer", 0)),
            is_bot=bool(p.get("isBot", False)),
            has_smite="smite" in _spell_raw(p).lower() or "golpear" in _spell_raw(p).lower(),
            spells=_spell_ids(p),
            spell_names=tuple(str(v.get("displayName", "")) for v in (p.get("summonerSpells") or {}).values()),
        )

    def _find_me(self, ap: dict, players: list[Player]) -> Player:
        ids = {str(ap.get(k)).lower() for k in ("riotId", "summonerName", "riotIdGameName") if ap.get(k)}
        for p in players:
            if p.names & ids:
                return p
        return players[0]

    def player_by_name(self, name: str | None, players: list[Player]) -> Player | None:
        if not name:
            return None
        n = name.lower()
        for p in players:
            if n in p.names:
                return p
        # bots aparecem às vezes só pelo nome do campeão
        for p in players:
            if p.champ_name.lower() == n or (p.champ_id or "").lower() == n:
                return p
        return None

    def _role(self, me: Player, profile: dict, in_pool: bool) -> str:
        cfg_role = str(self.cfg["jogador"].get("rota", "auto")).upper()
        if cfg_role in ROLES:
            return cfg_role
        if me.position in ROLES:
            return me.position
        if me.has_smite:
            return "JUNGLE"
        prof_role = str(profile.get("rota", "auto")).upper()
        return prof_role if in_pool and prof_role in ROLES else "MIDDLE"

    def _pick_opponent(self, role: str, enemies: list[Player]) -> Player | None:
        if self.opponent_key:
            for e in enemies:
                if e.key == self.opponent_key:
                    return e
        opp = next((e for e in enemies if e.position == role), None)
        if opp is None and role == "JUNGLE":
            opp = next((e for e in enemies if e.has_smite), None)
        if opp is None and len(enemies) == 1:
            opp = enemies[0]
        if opp is None and enemies and not any(e.position for e in enemies):
            # Sem posições (personalizada): quem não tem Smite e tem o mesmo índice na lista.
            laners = [e for e in enemies if not e.has_smite]
            order = ["TOP", "MIDDLE", "BOTTOM", "UTILITY"]
            if role in order and len(laners) >= 4:
                opp = laners[order.index(role)]
        if opp:
            self.opponent_key = opp.key
        return opp

    # ---------- atualização ----------
    def update(self, raw: dict) -> tuple[GameState, list[Change]]:
        gt = float(raw["gameData"].get("gameTime", 0))
        ap = raw["activePlayer"]
        players = [self._player(p) for p in raw.get("allPlayers", [])]
        me = self._find_me(ap, players)
        allies = [p for p in players if p.team == me.team]
        enemies = [p for p in players if p.team != me.team]
        profile, in_pool = champion_profile(self.champs, me.champ_id)
        role = self._role(me, profile, in_pool)
        opp = self._pick_opponent(role, enemies)
        stats = ap.get("championStats", {})
        abilities = {k: int(v.get("abilityLevel", 0)) for k, v in (ap.get("abilities") or {}).items()
                     if isinstance(v, dict) and "abilityLevel" in v}
        gold = float(ap.get("currentGold", 0))

        changes: list[Change] = []
        prev = self.prev
        first = prev is None
        if self.obj is None:
            self._init_objectives(str(raw["gameData"].get("gameMode", "")))

        # Eventos novos (abates, objetivos, etc.)
        for ev in (raw.get("events") or {}).get("Events", []):
            eid = int(ev.get("EventID", -1))
            if eid <= self.last_event_id:
                continue
            self.last_event_id = eid
            enriched = self._process_event(ev, players)
            # Ao entrar no meio da partida, eventos antigos só atualizam timers.
            if not first or float(ev.get("EventTime", 0)) > gt - 5:
                changes.append(Change("event", data=enriched))

        # Compra (=estou na base)
        if prev is not None:
            spent = prev.gold - gold
            new_items = Counter(me.items) - Counter(prev.me.items)
            # só item da loja: botas da runa e biscoitos aparecem fora da base (purchasable=False)
            shop = any((self.dd.item(i) or {}).get("gold", {}).get("purchasable", False) for i in new_items)
            if new_items and shop and (me.item_value > prev.me.item_value or spent > 30):
                self.last_purchase_t = gt
                changes.append(Change("purchase", me, {"spent": max(0.0, spent), "new_items": list(new_items)}))

        state = GameState(
            game_time=gt, me=me, allies=allies, enemies=enemies, opponent=opp, role=role,
            stats=stats, gold=gold, hp=float(stats.get("currentHealth", 0)),
            max_hp=float(stats.get("maxHealth", 1) or 1), abilities=abilities,
            objectives=self.obj, profile=profile, in_pool=in_pool,
            last_purchase_t=self.last_purchase_t,
            game_mode=str(raw["gameData"].get("gameMode", "")),
        )

        if prev is not None:
            prev_by_key = {p.key: p for p in prev.allies + prev.enemies}
            for p in players:
                old = prev_by_key.get(p.key)
                if not old:
                    continue
                if p.level > old.level:
                    changes.append(Change("level_up", p, {"level": p.level, "old": old.level}))
                for iid in Counter(p.completed) - Counter(old.completed):
                    changes.append(Change("item_completed", p, {"item": iid}))
            if me.is_dead and not prev.me.is_dead:
                killer = None
                for k in reversed(self.kill_log):
                    if k["victim"] is me or (k["victim"] and k["victim"].key == me.key):
                        killer = k["killer"]
                        break
                changes.append(Change("my_death", me, {"killer": killer, "respawn": me.respawn}))
            if not me.is_dead and prev.me.is_dead:
                changes.append(Change("my_respawn", me))

        self.prev = state
        return state, changes

    def _init_objectives(self, mode: str) -> None:
        """Tempos do config.yaml, com os ajustes do modo (ex.: SWIFTPLAY) por cima."""
        oc = dict(self.cfg["objetivos"])
        oc.update((self.cfg.get("objetivos_por_modo") or {}).get(mode, {}) or {})
        self.oc = oc
        self.obj = Objectives(
            next_dragon=oc["dragao_primeiro_s"],
            next_baron=oc["barao_s"],
            next_herald=oc.get("arauto_s"),
            next_grubs=oc.get("vastilarvas_s"),
            soul_at=int(oc.get("dragoes_para_alma", 4)),
        )

    def _elder_time(self, t: float) -> float:
        """Ancião nasce no horário mínimo do modo (15:00 no Swiftplay) se a alma sair antes; senão, respawn normal."""
        minimo = float(self.oc.get("anciao_minimo_s", 0))
        return minimo if t < minimo else t + self.oc["anciao_respawn_s"]

    def _team_of(self, name: str | None, players: list[Player]) -> str | None:
        p = self.player_by_name(name, players)
        return p.team if p else None

    def _process_event(self, ev: dict, players: list[Player]) -> dict:
        name = ev.get("EventName", "")
        t = float(ev.get("EventTime", 0))
        oc = self.oc
        out = dict(ev)
        killer = self.player_by_name(ev.get("KillerName"), players)
        out["killer"] = killer
        out["killer_team"] = killer.team if killer else None
        if name == "ChampionKill":
            victim = self.player_by_name(ev.get("VictimName"), players)
            out["victim"] = victim
            self.kill_log.append({"t": t, "killer": killer, "victim": victim})
        elif name == "DragonKill":
            if str(ev.get("DragonType", "")).lower() == "elder":
                self.obj.next_dragon = self._elder_time(t)
            else:
                if killer:
                    self.obj.dragons[killer.team] += 1
                if any(c >= self.obj.soul_at for c in self.obj.dragons.values()):
                    self.obj.elder_next = True
                    self.obj.next_dragon = self._elder_time(t)
                else:
                    self.obj.next_dragon = t + oc["dragao_respawn_s"]
        elif name == "BaronKill":
            self.obj.next_baron = t + oc["barao_respawn_s"]
            self.obj.baron_buff_team = out["killer_team"]
            self.obj.baron_buff_until = t + 180
        elif name == "HeraldKill":
            self.obj.next_herald = None
        elif name == "HordeKill":
            self.grubs_killed += 1
            if self.grubs_killed >= 3:
                self.obj.next_grubs = None
        return out


def lane_advantage(s: GameState) -> tuple[float, list[str]]:
    """Pontuação aproximada da minha vantagem contra o oponente de lane.
    Sem vida/cooldown do inimigo: assume ele com vida cheia."""
    o = s.opponent
    if o is None:
        return 0.0, []
    reasons = []
    lvl = s.me.level - o.level
    w = 1.5 if max(s.me.level, o.level) <= 3 else 1.0
    score = lvl * w
    if lvl:
        reasons.append(f"nível {lvl:+d}")
    gold = s.me.item_value - o.item_value
    score += gold / 400
    if abs(gold) >= 200:
        reasons.append(f"itens {gold:+d} de ouro")
    if s.me.level >= 6 > o.level:
        score += 1.5
        reasons.append("você tem ult, ele não")
    elif o.level >= 6 > s.me.level:
        score -= 1.5
        reasons.append("ele tem ult, você não")
    spike_items = int(s.profile.get("itens_spike", 1))
    if len(s.me.completed) >= spike_items > len(o.completed):
        score += 0.75
        reasons.append("seu spike de item")
    if s.me.level in s.profile.get("niveis_fortes", []) or s.me.level >= int(s.profile.get("allin_nivel", 99)):
        score += 0.5
    hp_term = (s.hp_pct - 0.75) * 3
    score += hp_term
    if s.hp_pct < 0.5:
        reasons.append(f"sua vida {s.hp_pct:.0%}")
    return round(score, 2), reasons
