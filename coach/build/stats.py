"""Atributos de campeões (base + nível + itens), banco de itens e fórmulas de mitigação."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field, replace

from ..ddragon import DataDragon, _norm
from .cdragon import CommunityDragon


# ---------------- mitigação ----------------
def resist_after(resist: float, flat_red: float = 0, pct_red: float = 0, pct_pen: float = 0,
                 flat_pen: float = 0) -> float:
    """Ordem do jogo: redução fixa -> redução % -> penetração % -> penetração fixa (letalidade).
    Redução pode deixar negativo; penetração não passa de zero."""
    r = resist - flat_red
    if r > 0:
        r *= 1 - pct_red
        r *= 1 - pct_pen
        r = max(0.0, r - flat_pen)
    return r


def dmg_mult(resist: float) -> float:
    """Dano × 100/(100+resistência); resistência negativa amplifica."""
    return 100 / (100 + resist) if resist >= 0 else 2 - 100 / (100 - resist)


def stack_pct(a: float, b: float) -> float:
    """Penetrações % se combinam de forma multiplicativa."""
    return 1 - (1 - a) * (1 - b)


# ---------------- atributos ----------------
@dataclass
class Stats:
    level: int = 1
    hp: float = 0
    base_hp: float = 0
    armor: float = 0
    base_armor: float = 0
    mr: float = 0
    base_mr: float = 0
    ad: float = 0
    base_ad: float = 0
    ap: float = 0
    ah: float = 0
    lethality: float = 0
    armor_pen_pct: float = 0
    magic_pen: float = 0
    magic_pen_pct: float = 0
    crit: float = 0
    crit_dmg: float = 1.75
    as_pct: float = 0
    lifesteal: float = 0
    omnivamp: float = 0
    melee: bool = True
    item_ids: list[int] = field(default_factory=list)

    @property
    def bonus_ad(self) -> float:
        return self.ad - self.base_ad

    @property
    def bonus_hp(self) -> float:
        return self.hp - self.base_hp

    @property
    def bonus_armor(self) -> float:
        return self.armor - self.base_armor

    @property
    def bonus_mr(self) -> float:
        return self.mr - self.base_mr

    def get(self, stat: str, formula: int = 0) -> float:
        """formula: 0 total, 1 base, 2 bônus (como no CommunityDragon)."""
        base = {"hp": self.base_hp, "armor": self.base_armor, "mr": self.base_mr, "ad": self.base_ad}.get(stat, 0.0)
        total = getattr(self, stat)
        return total if formula == 0 else base if formula == 1 else total - base

    def with_items(self, add: dict[str, float], remove: dict[str, float] | None = None) -> "Stats":
        s = replace(self, item_ids=list(self.item_ids))
        for sign, d in ((1, add), (-1, remove or {})):
            for k, v in d.items():
                if k in ("armor_pen_pct", "magic_pen_pct"):
                    cur = getattr(s, k)
                    setattr(s, k, stack_pct(cur, v) if sign > 0 else 1 - (1 - cur) / max(1e-9, 1 - v))
                elif hasattr(s, k):
                    setattr(s, k, getattr(s, k) + sign * v)
        s.crit = min(1.0, s.crit)
        return s


def _grow(base: float, per: float, lvl: int) -> float:
    g = lvl - 1
    return base + per * g * (0.7025 + 0.0175 * g)


def base_stats(dd: DataDragon, cid: str, level: int) -> Stats:
    st = dd.champions[cid]["stats"]
    hp = _grow(st["hp"], st["hpperlevel"], level)
    ar = _grow(st["armor"], st["armorperlevel"], level)
    mr = _grow(st["spellblock"], st["spellblockperlevel"], level)
    ad = _grow(st["attackdamage"], st["attackdamageperlevel"], level)
    return Stats(level=level, hp=hp, base_hp=hp, armor=ar, base_armor=ar, mr=mr, base_mr=mr,
                 ad=ad, base_ad=ad, melee=st.get("attackrange", 550) <= 300)


# ---------------- itens ----------------
@dataclass
class Item:
    id: int
    name: str
    name_en: str
    cost: int
    stats: dict[str, float]
    values: dict[str, float]
    groups: list[str]
    tags: list[str]
    components: list[int]
    completed: bool
    boots: bool
    passives: list[str]      # nomes de passivas/ativas no texto do item (pt_BR)

    @property
    def grievous(self) -> float:
        return self.values.get("GrievousAmount", 0.0)


class ItemDB:
    def __init__(self, dd: DataDragon, cd: CommunityDragon):
        self.dd, self.cd = dd, cd
        self.items: dict[int, Item] = {}
        for iid, d in dd.items.items():
            b = cd.items.get(iid)
            if not b:
                continue
            desc = d.get("description", "")
            self.items[iid] = Item(
                id=iid, name=d["name"], name_en=d["name_en"], cost=int(d["gold"]["total"]),
                stats=dict(b["stats"]), values=dict(b["values"]), groups=b["groups"],
                tags=d.get("tags", []), components=[int(x) for x in d.get("from", [])],
                completed=dd.is_completed(iid), boots=dd.is_boots(iid),
                passives=re.findall(r"<(?:passive|active)>([^<]+)</(?:passive|active)>", desc),
            )

    def get(self, iid: int) -> Item | None:
        return self.items.get(int(iid))

    def stats_of(self, ids: list[int]) -> dict[str, float]:
        tot: dict[str, float] = {}
        for i in ids:
            it = self.get(i)
            if not it:
                continue
            for k, v in it.stats.items():
                tot[k] = stack_pct(tot.get(k, 0), v) if k.endswith("_pct") and "pen" in k else tot.get(k, 0) + v
        return tot

    def consumed_components(self, iid: int, inventory: list[int]) -> list[int]:
        """Componentes do inventário que seriam gastos pra montar o item."""
        pool = Counter(inventory)
        used: list[int] = []

        def walk(cid: int) -> None:
            it = self.get(cid)
            if not it:
                return
            for comp in it.components:
                if pool[comp] > 0:
                    pool[comp] -= 1
                    used.append(comp)
                else:
                    walk(comp)

        walk(iid)
        return used

    def remaining_cost(self, iid: int, inventory: list[int]) -> int:
        return self.get(iid).cost - sum(self.get(c).cost for c in self.consumed_components(iid, inventory))

    def group_conflict(self, iid: int, owned: list[int]) -> bool:
        """Itens do mesmo grupo limitado (ex.: Último Sussurro, botas) não acumulam."""
        it = self.get(iid)
        rest = Counter(owned) - Counter(self.consumed_components(iid, owned))
        for g in it.groups:
            limit = self.cd.groups.get(g, 0)
            if limit and sum(n for o, n in rest.items() if self.get(o) and g in self.get(o).groups) >= limit:
                return True
        return False

    def candidates(self, owned: list[int], champ_ranged: bool) -> list[Item]:
        out = []
        seen_names = set()
        for iid, it in sorted(self.items.items()):
            d = self.dd.items[iid]
            if iid >= 10000 or not it.completed or iid in owned:
                continue
            if not d.get("maps", {}).get("11") or not d["gold"].get("purchasable", True):
                continue
            if d.get("requiredChampion") or d.get("requiredAlly") or d.get("inStore") is False:
                continue
            if not self.cd.items[iid].get("in_store", True):
                continue
            if _norm(it.name_en) in seen_names or self.group_conflict(iid, owned):
                continue
            # bota tier 3 só como upgrade da tier 2 que você já tem
            if it.boots and any(self.get(c) and self.get(c).boots and self.get(c).completed and c not in owned
                                for c in it.components):
                continue
            seen_names.add(_norm(it.name_en))
            out.append(it)
        return out
