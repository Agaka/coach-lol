"""Simulação de um combo contra um alvo (vida cheia, ou a vida inicial que você passar).

A sequência de cada combo é escrita à mão; os números de cada habilidade saem das
fórmulas do patch (calc.py). Passivas de item suportadas ficam em Fight."""
from __future__ import annotations

from collections import Counter

from ..ddragon import _norm
from .calc import Spell
from .cdragon import CommunityDragon
from .stats import Item, Stats, dmg_mult, resist_after

PHYS, MAGIC, TRUE = "físico", "mágico", "verdadeiro"

# Passivas de item modeladas (nome em inglês normalizado). O resto vira nota "não modelada".
MODELED = {
    "blackcleaver", "lorddominiksregards", "eclipse", "bladeoftheruinedking", "liandrystorment",
    "titanichydra", "spearofshojin", "thecollector", "kaenicrookern", "mawofmalmortius", "voltaiccyclosword",
}


class Fight:
    def __init__(self, me: Stats, target: Stats, items: list[Item], start_hp: float | None = None):
        self.me, self.t = me, target
        self.items = {_norm(i.name_en): i for i in items}
        self.hp_left = target.hp if start_hp is None else start_hp
        self.dealt = 0.0
        self.breakdown: Counter[str] = Counter()
        self.armor_shred_pct = 0.0
        self.hits = 0
        self.phys_hits = 0
        self.spellblade_ready = False
        self.burned = False
        self.eclipse_done = False
        # Cicloespada Voltaica: habilidade com dano energiza; o próximo ataque tira % da vida atual e dá letalidade
        self.cyclo = self.items.get("voltaiccyclosword")
        self.cyclo_ready = False
        self.cyclo_done = False
        self.lethality_mult = 1.0
        spellblade = [i for i in items if "SpellbladeMultiplier" in i.values]
        self.spellblade = spellblade[0] if spellblade else None
        self.amp = 0.0
        ldr = self.items.get("lorddominiksregards")
        if ldr:
            diff = max(0.0, min(1.0, (target.hp - me.hp) / ldr.values.get("MaxBonusHealth", 1500)))
            self.amp = ldr.values.get("MaxBonusDamagePercent", 0) * diff
        sj = self.items.get("spearofshojin")
        self.ability_amp = 0.0
        if sj:
            self.ability_amp = sj.values.get("SpellDamageIncrease", 0) * sj.values.get("StackCount", 0) * (
                1 if me.melee else sj.values.get("RangedMod", 1))

    @property
    def killed(self) -> bool:
        return self.hp_left <= 0

    @property
    def pct(self) -> float:
        return min(1.0, self.ratio)

    @property
    def ratio(self) -> float:
        """Dano / vida sem teto: acima de 1 é folga pra matar mesmo com escudo, cura ou item defensivo."""
        return self.dealt / self.t.hp if self.t.hp else 0.0

    def _mitigate(self, kind: str, raw: float) -> float:
        if kind == PHYS:
            r = resist_after(self.t.armor, 0, self.armor_shred_pct, self.me.armor_pen_pct,
                             self.me.lethality * self.lethality_mult)
            return raw * dmg_mult(r)
        if kind == MAGIC:
            r = resist_after(self.t.mr, 0, 0, self.me.magic_pen_pct, self.me.magic_pen)
            return raw * dmg_mult(r)
        return raw

    def deal(self, label: str, kind: str, raw: float, ability: bool = False) -> float:
        raw *= 1 + self.amp
        if ability:
            raw *= 1 + self.ability_amp
        dmg = self._mitigate(kind, raw)
        self.hp_left -= dmg
        self.dealt += dmg
        self.breakdown[label] += dmg
        self.hits += 1
        if kind == PHYS:
            self.phys_hits += 1
            bc = self.items.get("blackcleaver")
            if bc:
                mod = 1 if self.me.melee else bc.values.get("RangedMod", 1)
                stacks = min(self.phys_hits, bc.values.get("MaxStacks", 0))
                self.armor_shred_pct = max(self.armor_shred_pct, stacks * bc.values.get("ShredPerStack", 0) * mod)
        ec = self.items.get("eclipse")
        if ec and not self.eclipse_done and self.hits >= 2:
            self.eclipse_done = True
            pct = ec.values.get("MeleePercMaxHP", 0) * (1 if self.me.melee else ec.values.get("RangedPercMaxHPMult", 1))
            self.deal("Eclipse", PHYS, pct * self.t.hp)
        return dmg

    def ability(self, label: str, kind: str, raw: float) -> float:
        dmg = self.deal(label, kind, raw, ability=True)
        if self.spellblade:
            self.spellblade_ready = True
        if self.cyclo and not self.cyclo_done:
            self.cyclo_ready = True
        li = self.items.get("liandrystorment")
        if li and not self.burned:
            self.burned = True
            burn = li.values.get("BurnPercentHealthDamage", 0) * li.values.get("BurnDuration", 0) * self.t.hp
            self.deal("Liandry", MAGIC, burn)
        return dmg

    def auto(self, n: int = 1, label: str = "Ataques", raw: float | None = None) -> None:
        for _ in range(n):
            base = self.me.ad if raw is None else raw
            self.deal(label, PHYS, base * (1 + self.me.crit * (self.me.crit_dmg - 1)))
            self.on_hit()

    def on_hit(self) -> None:
        bk = self.items.get("bladeoftheruinedking")
        if bk:
            pct = bk.values.get("MeleeValue" if self.me.melee else "RangedValue", 0)
            self.deal("Lâmina do Rei Destruído", PHYS, pct * max(0.0, self.hp_left))
        th = self.items.get("titanichydra")
        if th:
            r = th.values.get("PrimaryTargetHPRatio", 0) * (1 if self.me.melee else th.values.get("RangedEffectiveness", 1))
            self.deal("Hidra Titânica", PHYS, r * self.me.hp)
        if self.spellblade_ready:
            self.spellblade_ready = False
            self.deal(self.spellblade.name, PHYS, self.spellblade.values["SpellbladeMultiplier"] * self.me.base_ad)
        if self.cyclo_ready:
            self.cyclo_ready, self.cyclo_done = False, True
            v = self.cyclo.values
            pct = v.get("PercentCurrentHPMelee" if self.me.melee else "PercentCurrentHPRanged", 0) / 100
            self.deal("Cicloespada (ataque energizado)", PHYS, pct * max(0.0, self.hp_left))
            self.lethality_mult = 1 + v.get("LethalityBonusModMelee" if self.me.melee else "LethalityBonusModRanged", 0) / 100

    def finish(self) -> None:
        col = self.items.get("thecollector")
        if col and self.hp_left > 0 and self.hp_left <= col.values.get("ExecuteThreshold", 0) * self.t.hp:
            self.breakdown["Coletor (execução)"] += self.hp_left
            self.dealt += self.hp_left
            self.hp_left = 0


# ---------------- combos da pool ----------------
class Kit:
    def __init__(self, cd: CommunityDragon, cid: str):
        self.spells = cd.champion(cid)

    def __getitem__(self, name: str) -> Spell:
        if name not in self.spells:
            from .calc import UnknownFormula
            raise UnknownFormula(f"habilidade '{name}' não existe no patch")
        return Spell(name, self.spells[name])


def combo_zed(f: Fight, k: Kit, r: dict) -> str:
    me, t = f.me, f.t
    shadows = (1 if r["W"] else 0) + (1 if r["R"] else 0)
    mark_start = f.dealt
    if r["E"]:
        f.ability("E", PHYS, k["ZedE"].calc("TotalDamage", r["E"], me))
    if r["Q"]:
        f.ability("Q", PHYS, k["ZedQ"].calc("TotalDamage", r["Q"], me))
        for _ in range(shadows):  # shurikens extras no mesmo alvo causam o dano reduzido
            f.ability("Q (sombras)", PHYS, k["ZedQ"].calc("PassThroughDamage", r["Q"], me))
    f.auto(2)
    p = k["ZedPassive"]
    if f.hp_left < p.value("CurrentHealthThreshold", 1) * t.hp:
        f.deal("Passiva (% vida)", MAGIC, p.calc("TotalDamage", 1, me, t))
    if r["R"]:
        R = k["ZedR"]
        f.ability("R (Marca da Morte)", PHYS,
                  R.calc("RCalculatedDamage", r["R"], me) + R.value("RDamageAmp", r["R"]) * (f.dealt - mark_start))
    return "R → E → Q com sombras → 2 ataques → passiva → estouro da marca"


def combo_warwick(f: Fight, k: Kit, r: dict) -> str:
    me, t = f.me, f.t
    if r["R"]:
        f.ability("R", MAGIC, k["WarwickR"].calc("DamageCumulative", r["R"], me))
    if r["Q"]:
        Q = k["WarwickQ"]
        f.ability("Q", MAGIC, Q.calc("BaseBiteDamage", r["Q"], me) + Q.value("TargetPercentHPDamage", r["Q"]) / 100 * t.hp)
        f.on_hit()
    onhit = k["WarwickP"].calc("OnHitDamage", 1, me)
    for _ in range(4):
        f.auto(1)
        f.deal("Passiva (ao acertar)", MAGIC, onhit)
    return "R → Q → 4 ataques com passiva"


def combo_garen(f: Fight, k: Kit, r: dict) -> str:
    me, t = f.me, f.t
    if r["Q"]:
        f.auto(1, "Q", k["GarenQ"].calc("TotalDamage", r["Q"], me))
    if r["E"]:
        E = k["GarenE"]
        ticks = int(E.value("NumTicks", r["E"]) + me.as_pct // E.value("ASPerTick", r["E"]))
        per = E.calc("TotalDamage", r["E"], me)
        for i in range(ticks):
            f.ability("E", PHYS, per)
            if i + 1 == int(E.value("StacksToShred", r["E"])):
                f.armor_shred_pct = max(f.armor_shred_pct, E.value("ShredAmount", r["E"]))
    f.auto(1)
    if r["R"]:
        R = k["GarenR"]
        missing = t.hp - max(0.0, f.hp_left)
        f.ability("R (verdadeiro)", TRUE, R.value("BaseDamage", r["R"]) + R.value("ExecuteDamage", r["R"]) * missing)
    return "Q → E completo (com redução de armadura) → ataque → R na vida perdida"


def combo_missfortune(f: Fight, k: Kit, r: dict) -> str:
    me = f.me
    if r["Q"]:
        f.ability("Q", PHYS, k["MissFortuneRicochetShot"].calc("TotalDamageTooltip", r["Q"], me))
        f.on_hit()
    if r["E"]:
        f.ability("E", MAGIC, k["MissFortuneScattershot"].calc("TotalDamage", r["E"], me))
    f.auto(1)
    f.deal("Passiva (Toque de Amor)", PHYS, k["MissFortunePassive"].calc("TotalDamage", 1, me))
    f.auto(3)
    if r["R"]:
        R = k["MissFortuneBulletTime"]
        waves = R.value("BaseWaves", r["R"]) * 0.5  # metade das ondas acertando o mesmo alvo
        normal = R.calc("PhysicalDamagePerWave", r["R"], me)
        crit = R.calc("CritDamagePerWave", r["R"], me)
        f.ability("R (metade das ondas)", PHYS, waves * ((1 - me.crit) * normal + me.crit * crit))
    return "Q → E → 4 ataques com passiva → R (metade das ondas)"


def combo_leona(f: Fight, k: Kit, r: dict) -> str:
    me = f.me
    if r["E"]:
        f.ability("E", MAGIC, k["LeonaZenithBlade"].calc("TotalDamageTooltip", r["E"], me))
    if r["Q"]:
        f.ability("Q", MAGIC, k["LeonaShieldOfDaybreak"].calc("TotalDamageTooltip", r["Q"], me))
        f.auto(1)
    if r["W"]:
        f.ability("W", MAGIC, k["LeonaSolarBarrier"].calc("TotalDamageTooltip", r["W"], me))
    if r["R"]:
        f.ability("R", MAGIC, k["LeonaSolarFlare"].calc("ExplosionCalculatedDamage", r["R"], me))
    f.deal("Passiva (Luz do Sol)", MAGIC, k["LeonaSunlightPassive"].calc("TotalDamage", 1, me))
    f.auto(1)
    return "E → Q → ataque → W → R → passiva → ataque"


def combo_gragas(f: Fight, k: Kit, r: dict) -> str:
    me, t = f.me, f.t
    if r["E"]:
        f.ability("E (barrigada)", MAGIC, k["GragasE"].calc("TotalDamage", r["E"], me))
    if r["Q"]:   # barril detonado logo depois do atordoamento: dano mínimo, sem carregar
        f.ability("Q (barril)", MAGIC, k["GragasQ"].calc("MinDamage", r["Q"], me))
    if r["W"]:   # próximo ataque com a bebida: dano mágico com % da vida máxima do alvo
        W = k["GragasW"]   # TotalDamage é só base + AP; a % da vida máxima vem num valor à parte
        f.ability("W (ataque com a bebida)", MAGIC,
                  W.calc("TotalDamage", r["W"], me, t) + W.value("MaxHPPercentDamage", r["W"]) / 100 * t.hp)
    f.auto(1)
    if r["R"]:
        f.ability("R (barril explosivo)", MAGIC, k["GragasR"].calc("DamageDone", r["R"], me))
    return "E (barrigada) → Q (barril) → W com ataque → R (barril explosivo)"


def combo_malzahar(f: Fight, k: Kit, r: dict) -> str:
    """Conservador: os voidlings do W e a área do R ficam de fora (fórmula do patch sem unidade clara)."""
    me, t = f.me, f.t
    if r["E"]:
        f.ability("E (maldição)", MAGIC, k["MalzaharE"].calc("TotalDamage", r["E"], me, t))
    if r["Q"]:
        f.ability("Q (portais)", MAGIC, k["MalzaharQ"].calc("TotalDamageTooltip", r["Q"], me, t))
    if r["R"]:
        f.ability("R (supressão)", MAGIC, k["MalzaharR"].calc("TotalDamageTooltip", r["R"], me, t))
    f.auto(1)
    return "E (maldição) → Q (portais) → R (supressão) → ataque; sem os voidlings do W"


def combo_mel(f: Fight, k: Kit, r: dict) -> str:
    """Conservador: metade dos projéteis seguintes do Q acertam o alvo; a execução do Deslumbrar fica de fora.
    O TotalDamageTT do Q no CommunityDragon pede 'COUNT' e o dado se chama 'Count', então o Q sai dos valores."""
    me, t = f.me, f.t
    hits = 0
    casts = 0
    if r["E"]:
        f.ability("E (armadilha)", MAGIC, k["MelE"].calc("Damage", r["E"], me, t))
        hits, casts = hits + 1, casts + 1
    if r["Q"]:
        Q = k["MelQ"]
        extra = (Q.value("Count", r["Q"]) - 1) / 2
        sub = Q.value("BaseDamageSubsequent", r["Q"]) + Q.value("APRatioSubsuquent", r["Q"]) * me.ap
        f.ability("Q (rajada)", MAGIC, Q.calc("InitialDamage", r["Q"], me, t) + extra * sub)
        hits, casts = hits + 1 + int(extra), casts + 1
    P = k["MelPassive"]
    missiles = min(P.value("MaxPassiveBonusMissiles", 1), P.value("PassiveBonusMissiles", 1) * casts)
    f.auto(1)
    if missiles:
        f.deal("Passiva (projéteis extras)", MAGIC, missiles * P.calc("PassiveBonusMissileDamage", 1, me, t))
    hits += 1 + int(missiles)
    if r["R"]:
        R = k["MelR"]
        f.ability("R (eclipse)", MAGIC, R.calc("UltFlatDamage", r["R"], me, t) + hits * R.calc("UltStackDamage", r["R"], me, t))
    return "E (armadilha) → Q (rajada) → ataque com projéteis da passiva → R (eclipse nas marcas)"


def combo_generic(f: Fight, k: Kit | None, r: dict, magic: bool = False) -> str:
    """Fora da pool não há fórmula escrita: só ataques e 2 'habilidades' proporcionais aos atributos."""
    me = f.me
    autos, casts = (1, 3) if magic else (3, 2)
    f.auto(autos)
    scale = me.ap * 0.6 if magic else me.bonus_ad * 0.8
    for _ in range(casts):
        f.ability("Habilidade (genérica)", MAGIC if magic else PHYS, 40 + 25 * me.level / 3 + scale)
    return f"modelo genérico: {autos} ataque(s) + {casts} habilidades aproximadas"


COMBOS = {
    "Zed": combo_zed,
    "Warwick": combo_warwick,
    "Garen": combo_garen,
    "MissFortune": combo_missfortune,
    "Leona": combo_leona,
    "Gragas": combo_gragas,
    "Malzahar": combo_malzahar,
    "Mel": combo_mel,
}
