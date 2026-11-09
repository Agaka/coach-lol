"""Avaliador das fórmulas de habilidade do CommunityDragon (mSpellCalculations).
Os números (dano base por rank, escalas) vêm do patch; o código só sabe somar as partes."""
from __future__ import annotations

from .stats import Stats

# mStat do CommunityDragon -> atributo (ausente = 0 = poder de habilidade)
STAT = {0: "ap", 1: "armor", 2: "ad", 6: "mr", 9: "crit_dmg", 12: "hp"}
TARGET_FLAG = "{a8cb9c14}"           # parte calculada sobre o alvo (ex.: % da vida máxima dele)
CALC_REF = "{f3cbe7b2}"              # referência a outra fórmula da mesma habilidade


class UnknownFormula(Exception):
    pass


class Spell:
    def __init__(self, name: str, data: dict):
        self.name = name
        self.values: dict[str, list[float]] = data.get("values", {})
        self.calcs: dict[str, dict] = data.get("calcs", {})

    def value(self, key: str, rank: int) -> float:
        if key not in self.values:
            raise UnknownFormula(f"{self.name}: valor '{key}' não existe no patch")
        vals = self.values[key]
        return float(vals[min(max(rank, 0), len(vals) - 1)])

    def calc(self, key: str, rank: int, me: Stats, target: Stats | None = None) -> float:
        if key not in self.calcs:
            raise UnknownFormula(f"{self.name}: fórmula '{key}' não existe no patch")
        return _Eval(self, rank, me, target).calc(self.calcs[key])


class _Eval:
    def __init__(self, spell: Spell, rank: int, me: Stats, target: Stats | None):
        self.sp, self.rank, self.me, self.target = spell, rank, me, target

    def stat(self, part: dict, on_target: bool = False) -> float:
        who = self.target if on_target and self.target else self.me
        name = STAT.get(int(part.get("mStat", 0)))
        if name is None:
            raise UnknownFormula(f"{self.sp.name}: mStat {part.get('mStat')} desconhecido")
        return who.get(name, int(part.get("mStatFormula", 0)))

    def calc(self, c: dict) -> float:
        t = c.get("__type")
        if t == "GameCalculationModified":
            base = self.calc(self.sp.calcs[c["mModifiedGameCalculation"]])
            return base * self.part(c["mMultiplier"]) if "mMultiplier" in c else base
        if t == "GameCalculationConditional":
            key = c.get("mDefaultGameCalculation") or c.get("mConditionalGameCalculation")
            return self.calc(self.sp.calcs[key])
        total = sum(self.part(p) for p in c.get("mFormulaParts", []))
        if "mMultiplier" in c:
            total *= self.part(c["mMultiplier"])
        return total

    def part(self, p: dict) -> float:
        t = p.get("__type")
        lvl = self.me.level
        if t == "NumberCalculationPart":
            return float(p.get("mNumber", 0))
        if t == "NamedDataValueCalculationPart":
            return self.sp.value(p["mDataValue"], self.rank)
        if t == "StatByNamedDataValueCalculationPart":
            return self.stat(p) * self.sp.value(p["mDataValue"], self.rank)
        if t == "StatByCoefficientCalculationPart":
            return self.stat(p) * float(p.get("mCoefficient", 0))
        if t == "StatBySubPartCalculationPart":
            return self.stat(p, on_target=bool(p.get(TARGET_FLAG))) * self.part(p["mSubpart"])
        if t == CALC_REF:
            return self.calc(self.sp.calcs[p["mSpellCalculationKey"]])
        if t == "SumOfSubPartsCalculationPart":
            return sum(self.part(x) for x in p.get("mSubparts", []))
        if t == "ProductOfSubPartsCalculationPart":
            return self.part(p["mPart1"]) * self.part(p["mPart2"])
        if t == "ByCharLevelInterpolationCalculationPart":
            a, b = float(p.get("mStartValue", 0)), float(p.get("mEndValue", 0))
            return a + (b - a) * (min(lvl, 18) - 1) / 17
        if t == "ByCharLevelFormulaCalculationPart":
            vals = p.get("values", [])
            return float(vals[min(lvl, len(vals) - 1)]) if vals else 0.0
        if t == "ByCharLevelBreakpointsCalculationPart":
            v = float(p.get("mLevel1Value", 0))
            per = float(p.get("mInitialBonusPerLevel", 0))
            bps = {int(b["mLevel"]): b for b in p.get("mBreakpoints", [])}
            for L in range(2, lvl + 1):
                if L in bps:
                    v += float(bps[L].get("mAdditionalBonusAtThisLevel", 0))
                    per = float(bps[L].get("mBonusPerLevelAtAndAfter", per))
                v += per
            return v
        raise UnknownFormula(f"{self.sp.name}: parte de fórmula '{t}' não suportada")
