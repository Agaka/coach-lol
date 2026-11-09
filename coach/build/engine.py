"""Recomendação de item calculada: simula cada candidato contra cada inimigo e ranqueia
por ganho real por ouro. Toda explicação sai daqui, com os números do cálculo."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from ..config import champion_profile
from ..ddragon import DataDragon, _norm
from ..state import GameState, Player
from .calc import UnknownFormula
from .cdragon import CommunityDragon
from .combos import COMBOS, MODELED, Fight, Kit, combo_generic
from .stats import Item, ItemDB, Stats, base_stats, dmg_mult, resist_after


@dataclass
class Candidate:
    item: Item
    cost: int
    remaining: int
    dmg_before: dict[str, float]
    dmg_after: dict[str, float]
    dmg_gain: float              # % a mais de dano do combo, ponderado pelo alvo
    ehp_before: float
    ehp_after: float
    ehp_gain: float              # % de vida efetiva a mais
    antiheal: float              # pontos percentuais equivalentes
    score: float
    per_1000: float
    notes: list[str] = field(default_factory=list)
    forced: bool = False         # entra na lista mesmo fora do top (ativo que o cálculo não modela)

    @property
    def main_reason(self) -> str:
        parts = {"dano": self.dmg_gain * self._pd, "defesa": self.ehp_gain * self._pf, "anticura": self.antiheal * self._pd}
        return max(parts, key=parts.get)

    _pd: float = 1.0
    _pf: float = 0.5


@dataclass
class Recommendation:
    game_time: float
    reason: str                       # "morte" ou "base"
    ranking: list[Candidate]
    weights: dict[str, float]
    profile: dict[str, float]
    combo_desc: str
    gold: float
    buy_now: list[str]
    voice: str
    text: str
    main_target: str
    chosen_id: int | None = None      # item que virou fala (cálculo ou Sonnet)
    chosen_by: str = "cálculo"


class BuildAdvisor:
    def __init__(self, dd: DataDragon, champs: dict, cfg: dict):
        self.dd, self.champs, self.cfg = dd, champs, cfg
        self.cd = CommunityDragon(dd.version).load()
        self.idb = ItemDB(dd, self.cd)
        self.healers = {_norm(x) for x in cfg.get("build", {}).get("curadores", [])}
        self.history: list[Recommendation] = []

    # ---------------- atributos ----------------
    def my_stats(self, s: GameState) -> Stats:
        cid = s.me.champ_id
        st = base_stats(self.dd, cid, s.me.level).with_items(self.idb.stats_of(s.me.items))
        st.item_ids = list(s.me.items)
        api = s.stats or {}
        # A Live API já soma runas, fragmentos e nível: usa ela quando houver.
        for key, attr in (("attackDamage", "ad"), ("abilityPower", "ap"), ("armor", "armor"),
                          ("magicResist", "mr"), ("maxHealth", "hp"), ("abilityHaste", "ah")):
            if isinstance(api.get(key), (int, float)) and api[key] > 0:
                setattr(st, attr, float(api[key]))
        if isinstance(api.get("physicalLethality"), (int, float)):
            st.lethality = max(st.lethality, float(api["physicalLethality"]))
        if isinstance(api.get("critChance"), (int, float)):
            c = float(api["critChance"])
            st.crit = max(st.crit, c / 100 if c > 1 else c)
        return st

    def enemy_stats(self, p: Player) -> Stats:
        cid = p.champ_id or "Garen"
        st = base_stats(self.dd, cid, p.level).with_items(self.idb.stats_of(p.items))
        st.item_ids = list(p.items)
        return st

    def damage_profile(self, enemies: list[Player]) -> dict[str, float]:
        """Físico x mágico somado dos itens inimigos; sem itens, usa as notas do Data Dragon."""
        phys = magic = 0.0
        leth = pen_pct = mpen = mpen_pct = 0.0
        for e in enemies:
            it = self.idb.stats_of(e.items)
            info = self.dd.champions.get(e.champ_id or "", {}).get("info", {})
            phys += it.get("ad", 0) + 1.5 * it.get("lethality", 0) + 8 * info.get("attack", 5)
            magic += it.get("ap", 0) + 8 * info.get("magic", 5)
            leth += it.get("lethality", 0)
            pen_pct = max(pen_pct, it.get("armor_pen_pct", 0))
            mpen += it.get("magic_pen", 0)
            mpen_pct = max(mpen_pct, it.get("magic_pen_pct", 0))
        n = max(1, len(enemies))
        tot = phys + magic or 1
        return {"fisico": phys / tot, "magico": magic / tot, "letalidade": leth / n,
                "pen_armadura": pen_pct, "pen_magica": mpen / n, "pen_magica_pct": mpen_pct}

    def ehp(self, st: Stats, items: list[Item], prof: dict) -> float:
        ar = resist_after(st.armor, 0, 0, prof["pen_armadura"], prof["letalidade"])
        mr = resist_after(st.mr, 0, 0, prof["pen_magica_pct"], prof["pen_magica"])
        names = {_norm(i.name_en): i for i in items}
        shield_magic = 0.0
        kr = names.get("kaenicrookern")
        if kr:
            shield_magic += kr.values.get("ShieldAmount", 0) * st.hp
        maw = names.get("mawofmalmortius")
        if maw:
            mod = 1 if st.melee else maw.values.get("RangedShieldMod", 1)
            shield_magic += (maw.values.get("ShieldSize", 0) + maw.values.get("ShieldADScaling", 0) * st.bonus_ad) * mod
        ec = names.get("eclipse")
        shield = 0.0
        if ec:
            mod = 1 if st.melee else ec.values.get("RangedShieldMult", 1)
            shield += (ec.values.get("MeleeBaseShield", 0) + ec.values.get("MeleeBonusADShieldRatio", 0) * st.bonus_ad) * mod
        pa, pm = prof["fisico"] * dmg_mult(ar), prof["magico"] * dmg_mult(mr)
        hp = st.hp + shield
        total = hp / (pa + pm)
        if shield_magic:
            # escudo mágico só absorve a parte mágica: dano total D até morrer
            total = (hp + shield_magic) / (pa + pm)
            if shield_magic > total * pm:  # escudo nem chega a acabar
                total = hp / pa if pa > 0 else total
        return total

    # ---------------- combo ----------------
    def combo(self, cid: str, me: Stats, items: list[Item], target: Stats, ranks: dict,
              start_hp: float | None = None) -> tuple[float, Fight, str]:
        f = Fight(me, target, items, start_hp)
        fn = COMBOS.get(cid)
        try:
            desc = fn(f, Kit(self.cd, cid), ranks) if fn else combo_generic(
                f, None, ranks, magic=self.dd.champions[cid]["info"].get("magic", 0) > self.dd.champions[cid]["info"].get("attack", 0))
        except UnknownFormula as e:
            f = Fight(me, target, items, start_hp)
            desc = f"genérico ({e})"
            combo_generic(f, None, ranks)
        f.finish()
        return f.pct, f, desc

    def kill_lines(self, s: GameState) -> dict[str, tuple[float, float]]:
        """Por inimigo: (maior % de vida em que o combo ainda mata, dano/vida cheia).
        A Live API não informa a vida atual dos inimigos: você compara com a barra na tela."""
        cid = s.me.champ_id
        if not cid or not s.enemies:
            return {}
        me = self.my_stats(s)
        items = [self.idb.get(i) for i in s.me.items if self.idb.get(i)]
        ranks = {k: int(s.abilities.get(k, 0)) for k in ("Q", "W", "E", "R")}
        out = {}
        for e in s.enemies:
            st = self.enemy_stats(e)
            _, f, _ = self.combo(cid, me, items, st, ranks)
            if f.ratio >= 1:
                out[e.key] = (1.0, f.ratio)
                continue
            lo, hi = 0.0, 1.0   # mata começando com lo; não mata com hi (passivas de execução não são lineares)
            for _ in range(8):
                mid = (lo + hi) / 2
                if self.combo(cid, me, items, st, ranks, start_hp=mid * st.hp)[1].killed:
                    lo = mid
                else:
                    hi = mid
            out[e.key] = (lo, f.ratio)
        return out

    def weights(self, s: GameState, profile: dict, est: dict[str, Stats]) -> dict[str, float]:
        alvo = profile.get("alvo", "generico")
        raw = {}
        golds = [e.item_value for e in s.enemies] or [1]
        avg_gold = max(1, sum(golds) / len(golds))
        avg_ehp = sum(st.hp * (1 + st.armor / 100) for st in est.values()) / max(1, len(est))
        for e in s.enemies:
            st = est[e.key]
            if alvo == "carregadores_frageis":
                carry = 1.5 if e.position in ("BOTTOM", "MIDDLE") else 1.0
                raw[e.key] = carry * (0.5 + e.item_value / avg_gold / 2) * avg_ehp / (st.hp * (1 + st.armor / 100))
            else:
                raw[e.key] = 1.0
        tot = sum(raw.values()) or 1
        return {k: v / tot for k, v in raw.items()}

    # ---------------- recomendação ----------------
    def recommend(self, s: GameState, reason: str) -> Recommendation | None:
        if not s.enemies or not s.me.champ_id:
            return None
        cid = s.me.champ_id
        profile, in_pool = champion_profile(self.champs, cid)
        if not in_pool and not profile.get("build_proibe"):
            profile = dict(profile, **self._outside_pool_profile(cid, s.role))
        pd, pf = float(profile.get("peso_dano", 1.0)), float(profile.get("peso_defesa", 0.5))
        me = self.my_stats(s)
        owned_items = [self.idb.get(i) for i in s.me.items if self.idb.get(i)]
        ranks = {k: int(s.abilities.get(k, 0)) for k in ("Q", "W", "E", "R")}
        if not any(ranks.values()):
            ranks = {"Q": 1, "W": 0, "E": 0, "R": 0}
        est = {e.key: self.enemy_stats(e) for e in s.enemies}
        names = {e.key: e.champ_name for e in s.enemies}
        prof = self.damage_profile(s.enemies)
        w = self.weights(s, profile, est)
        main_key = max(w, key=w.get)

        before, before_raw, combo_desc = {}, {}, ""
        for k, st in est.items():
            before[k], f0, combo_desc = self.combo(cid, me, owned_items, st, ranks)
            before_raw[k] = f0.ratio
        ehp0 = self.ehp(me, owned_items, prof)
        has_gw = any(i.grievous for i in owned_items)
        heal_share = {e.key: self._heal_share(e) for e in s.enemies}

        ranking: list[Candidate] = []
        excluded = 0
        uses_mana = self.dd.champions[cid].get("partype", "Mana") in ("Mana", "Mana ")
        for it in self.idb.candidates(s.me.items, not me.melee):
            if not self._fits_champion(it, profile) or (not uses_mana and it.stats.get("mana", 0) > 0):
                excluded += 1
                continue
            consumed = self.idb.consumed_components(it.id, s.me.items)
            new_ids = list(s.me.items)
            for c in consumed:
                new_ids.remove(c)
            new_ids.append(it.id)
            me2 = me.with_items(it.stats, self.idb.stats_of(consumed))
            items2 = [self.idb.get(i) for i in new_ids if self.idb.get(i)]
            after, after_raw = {}, {}
            for k, st in est.items():
                after[k], f1, _ = self.combo(cid, me2, items2, st, ranks)
                after_raw[k] = f1.ratio
            # ganho relativo (% a mais de dano), sem teto de 100%: depois que o combo já mata,
            # dano extra ainda vale (escudos, curas e itens defensivos que o inimigo vai comprar)
            gain = sum(w[k] * (after_raw[k] / before_raw[k] - 1) for k in est if before_raw[k] > 0) * 100
            ehp1 = self.ehp(me2, items2, prof)
            ehp_gain = (ehp1 / ehp0 - 1) * 100
            anti = 0.0
            if it.grievous and not has_gw:
                anti = sum(w[k] * heal_share[k] for k in est) * it.grievous * 100
            score = pd * (gain + anti) + pf * ehp_gain
            notes = [f"passiva/ativa não modelada: {p}" for p in dict.fromkeys(it.passives)
                     if _norm(it.name_en) not in MODELED and not it.grievous and "SpellbladeMultiplier" not in it.values]
            if it.stats.get("ah"):
                notes.append(f"+{it.stats['ah']:.0f} aceleração (não entra no combo único)")
            # ganho por ouro que AINDA falta gastar: com os componentes na mão, o ganho medido já desconta
            # os atributos deles, então dividir pelo preço cheio afundava o item que você começou
            remaining = self.idb.remaining_cost(it.id, s.me.items)
            c = Candidate(it, it.cost, remaining, before, after, gain,
                          ehp0, ehp1, ehp_gain, anti, score, score / max(remaining, 300) * 1000, notes)
            c._pd, c._pf = pd, pf
            ranking.append(c)
        # meta de Mestre+: o que a maioria faz no campeão pesa no ranking (o cálculo sozinho preferia Glaive
        # Sombria no Zed pelos atributos no papel; 97% dos Zed de Mestre+ fazem Cicloespada)
        w_meta = float(self.cfg.get("build", {}).get("peso_meta", 0.3))
        if w_meta:
            for c, u in ((c, self._meta_usage(cid, s.role).get(c.item.id, 0.0)) for c in ranking):
                if u >= 0.1:
                    c.per_1000 *= 1 + w_meta * u
                    c.notes.insert(0, f"feito por {u:.0%} dos {self.dd.champ_name(cid)} em Mestre+: "
                                      f"+{w_meta * u:.0%} na pontuação")
        ranking.sort(key=lambda c: c.per_1000, reverse=True)
        if not ranking:
            return None
        # decide manter o anterior olhando a lista inteira; só depois corta no top
        kept = self._keep_previous(ranking, s.me.items)
        top_n = int(self.cfg.get("build", {}).get("top", 5))
        full = ranking
        ranking = ranking[:max(top_n, 1)]
        if self.history:   # o item recomendado da última vez continua na lista, mesmo fora do top
            last = self.history[-1]
            prev_id = last.chosen_id or last.ranking[0].item.id
            prev = next((c for c in full if c.item.id == prev_id), None)
            if prev is not None and prev not in ranking:
                ranking.append(prev)
        # ativo de estase (Ampulheta de Zhonya, Anjo Guardião) não entra no cálculo; contra assassino ele
        # anula o burst (ex.: a marca do R do Zed). Vai pra lista com a observação, quem pesa é o Sonnet.
        assassins = [e.champ_name for e in s.enemies
                     if (self.dd.champions.get(e.champ_id or "", {}).get("tags") or [""])[0] == "Assassin"]
        if assassins:
            for c in full:
                if "Estase" in (self.dd.item(c.item.id) or {}).get("description", ""):
                    c.notes.append(f"ativo de estase não entra no cálculo: anula o burst de {', '.join(assassins)}")
                    c.forced = True
                    if c not in ranking:
                        ranking.append(c)
        best = ranking[0]
        buy_now = self._buy_now(best.item, s.me.items, s.gold)
        voice = self._voice(best, names[main_key], s, heal_share, w, names)
        rec = Recommendation(s.game_time, reason, ranking, {names[k]: v for k, v in w.items()}, prof, combo_desc,
                             s.gold, buy_now, voice, "", names[main_key])
        rec.text = self._explain(rec, s, names, est, me)
        if kept:
            rec.text += "\n" + kept
        if excluded:
            rec.text += f"\n{excluded} itens fora do perfil do campeão ignorados (build_exige/build_proibe no champions.yaml)."

        self.history.append(rec)
        return rec

    # ---------------- campeão fora da pool ----------------
    AD, AP, TANK = {"ad", "lethality", "armor_pen_pct", "crit", "as_pct"}, {"ap", "magic_pen", "magic_pen_pct"}, \
        {"hp", "armor", "mr"}

    def _outside_pool_profile(self, cid: str, role: str) -> dict:
        """Classe de item pra campeão fora da pool. Antes era só "mago x físico" e um Yorick recebia Glaive
        Sombria (item de assassino) toda partida. Agora: 1) o que o campeão faz no elo alto, se houver amostra;
        2) senão, a classe do Data Dragon."""
        meta = self._meta_stats(cid, role)
        if meta:
            return meta
        ch = self.dd.champions.get(cid, {})
        tags = ch.get("tags") or ["Fighter"]
        info = ch.get("info", {})
        mage = info.get("magic", 0) > info.get("attack", 0)
        main = tags[0]
        if main == "Mage" or (main in ("Tank", "Support") and mage) or (main == "Assassin" and mage):
            p = {"build_proibe": ["ad", "crit", "lethality", "as_pct", "armor_pen_pct"]}
            if main == "Tank":
                p.update(build_exige=["hp", "armor", "mr", "ap"], peso_dano=0.4, peso_defesa=0.8)
            return p
        if main == "Tank":
            return {"build_exige": ["hp", "armor", "mr"], "build_proibe": ["ap", "crit", "lethality", "magic_pen"],
                    "peso_dano": 0.3, "peso_defesa": 0.9}
        if main == "Support":
            return {"build_exige": ["hp", "armor", "mr"], "build_proibe": ["crit", "lethality", "as_pct"],
                    "peso_dano": 0.2, "peso_defesa": 0.9}
        if main == "Marksman":
            return {"build_exige": ["ad", "crit", "as_pct"], "build_proibe": ["ap", "magic_pen", "magic_pen_pct"],
                    "build_vida_max": 0}
        if main == "Assassin":
            return {"build_exige": ["ad", "lethality"], "build_proibe": ["ap", "magic_pen", "magic_pen_pct", "crit"]}
        # lutador: dano físico com vida/resistência; crítico só se também for assassino (Yasuo, Yone)
        proibe = ["ap", "magic_pen", "magic_pen_pct", "lethality"]
        if "Assassin" not in tags:
            proibe.append("crit")
        return {"build_exige": ["ad", "hp", "armor", "mr"], "build_proibe": proibe, "peso_dano": 0.7, "peso_defesa": 0.45}

    def _meta_usage(self, cid: str, role: str) -> dict[int, float]:
        """Fração das partidas de Mestre+ do campeão na rota em que cada item completo aparece."""
        elo = getattr(self, "elo", None)
        if not elo or not elo.available:
            return {}
        if not hasattr(self, "_meta_cache"):
            self._meta_cache = {}
        key = (cid, role)
        if key not in self._meta_cache:
            k = "".join(ch for ch in cid.lower() if ch.isalnum())
            games = ((elo.data.get("lane") or {}).get(f"{role}|{k}") or {}).get("n", 0)
            raw = (elo.data.get("itens") or {}).get(f"{role}|{k}") or {}
            self._meta_cache[key] = ({int(i): n / games for i, (n, _v) in raw.items()}
                                     if games >= int(elo.cfg["estatisticas"].get("amostra_minima", 20)) else {})
        return self._meta_cache[key]

    def _meta_stats(self, cid: str, role: str) -> dict | None:
        """Família de atributos dos itens que o campeão realmente faz em elo alto (Mestre+)."""
        elo = getattr(self, "elo", None)
        if not elo or not elo.available:
            return None
        k = "".join(ch for ch in cid.lower() if ch.isalnum())
        raw = (elo.data.get("itens") or {}).get(f"{role}|{k}")
        if not raw:   # rota diferente da mais comum: junta todas
            raw = {}
            for key, items in (elo.data.get("itens") or {}).items():
                if key.endswith(f"|{k}"):
                    for iid, (n, v) in items.items():
                        raw.setdefault(iid, [0, 0])
                        raw[iid][0] += n
        built = Counter()
        total = 0
        for iid, (n, _v) in raw.items():
            it = self.idb.get(int(iid))
            if not it or it.boots or not self.dd.is_completed(int(iid)):
                continue
            total += n
            for stat in it.stats:
                built[stat] += n
        if total < 20:
            return None
        share = {stat: c / total for stat, c in built.items()}
        proibe = [stat for stat in self.AD | self.AP if share.get(stat, 0) < 0.1]
        exige = [stat for stat in self.AD | self.AP | self.TANK if share.get(stat, 0) >= 0.3]
        tanky = sum(share.get(x, 0) for x in self.TANK) / 3
        return {"build_exige": exige, "build_proibe": proibe,
                "peso_defesa": round(0.3 + 0.6 * min(1.0, tanky), 2), "peso_dano": round(1.0 - 0.6 * min(1.0, tanky), 2)}

    @staticmethod
    def _fits_champion(it: Item, profile: dict) -> bool:
        """Filtro de classe do champions.yaml: o item precisa dar algum atributo de build_exige
        (botas são exceção), não pode dar nada de build_proibe e nem mais vida que build_vida_max."""
        st = it.stats
        need = profile.get("build_exige") or []
        if need and not it.boots and not any(st.get(k, 0) > 0 for k in need):
            return False
        if any(st.get(k, 0) > 0 for k in profile.get("build_proibe") or []):
            return False
        hp_max = profile.get("build_vida_max")
        return hp_max is None or st.get("hp", 0) <= float(hp_max)

    def _keep_previous(self, ranking: list[Candidate], inventory: list[int]) -> str:
        """Evita trocar de item a cada volta à base: mantém o recomendado anterior se ele continua
        perto do melhor (10%). Se você já comprou componentes dele, mantém até terminar: trocar no meio
        do item (Brutalizador comprado pra Cicloespada e o coach mandando Arco do Axioma) só desperdiça ouro."""
        if not self.history:
            return ""
        last = self.history[-1]
        prev_id = last.chosen_id or last.ranking[0].item.id     # o que foi falado (cálculo ou Sonnet)
        prev = next((c for c in ranking if c.item.id == prev_id), None)
        if prev is None or prev is ranking[0]:
            return ""
        # "começou" = componentes dele valendo 40%+ do preço (uma Espada Longa entra em metade dos itens de AD)
        owned = sum(self.idb.get(c).cost for c in self.idb.consumed_components(prev_id, inventory) if self.idb.get(c))
        started = owned >= 0.4 * prev.cost
        if not started and prev.per_1000 < ranking[0].per_1000 * 0.9:
            return ""
        best = ranking[0]
        ranking.remove(prev)
        ranking.insert(0, prev)
        why = "você já começou ele (tem componentes)" if started else "continua perto do melhor"
        return (f"Mantido {prev.item.name} ({prev.per_1000:.2f}) em vez de {best.item.name} ({best.per_1000:.2f}): "
                f"{why}, trocar de ideia agora custa mais do que a diferença.")

    def _heal_share(self, e: Player) -> float:
        it = self.idb.stats_of(e.items)
        share = it.get("lifesteal", 0) + it.get("omnivamp", 0)
        if _norm(e.champ_id or "") in self.healers or _norm(e.champ_name) in self.healers:
            share += 0.15
        return share

    def _buy_now(self, item: Item, inventory: list[int], gold: float) -> list[str]:
        """Com o ouro de agora: o item inteiro, ou os componentes mais caros que cabem."""
        if self.idb.remaining_cost(item.id, inventory) <= gold:
            return [item.name]
        out, left = [], gold
        missing = [c for c in self._missing_components(item.id, list(inventory))]
        for comp in sorted(missing, key=lambda c: -self.idb.get(c).cost):
            rc = self.idb.remaining_cost(comp, inventory)
            if rc <= left:
                out.append(self.idb.get(comp).name)
                left -= rc
        return out

    def _missing_components(self, iid: int, inventory: list[int]) -> list[int]:
        out = []
        for comp in self.idb.get(iid).components:
            if comp in inventory:
                inventory.remove(comp)
            else:
                out.append(comp)
        return out

    def _voice(self, c: Candidate, target: str, s: GameState, heal, w, names) -> str:
        name = c.item.name
        r = c.main_reason
        if r == "dano":
            # cita o alvo prioritário que o combo ainda não mata; se já mata todos eles, fala do ganho
            alive = [k for k in w if c.dmg_after[k] < 1.0 and w[k] >= 0.15]
            if alive:
                k = max(alive, key=w.get)
                return f"Próximo item: {name}. Combo tira {c.dmg_after[k]:.0%} de {names[k]}"
            return f"Próximo item: {name}. Mais {c.dmg_gain:.0f}% de dano no combo"
        if r == "anticura":
            k = max(heal, key=heal.get)
            return f"Próximo item: {name}. Corta a cura de {names[k]}"
        return f"Próximo item: {name}. Aguenta {c.ehp_gain:.0f}% mais dano"

    def _explain(self, rec: Recommendation, s: GameState, names, est, me: Stats) -> str:
        L = []
        mm = f"{int(rec.game_time) // 60:02d}:{int(rec.game_time) % 60:02d}"
        L.append(f"BUILD CALCULADA — {mm} ({'você morreu' if rec.reason == 'morte' else 'na base'}) — patch {self.dd.version}")
        L.append(f"{s.me.champ_name} nível {s.me.level} | AD {me.ad:.0f} (bônus {me.bonus_ad:.0f}) | AP {me.ap:.0f} | "
                 f"vida {me.hp:.0f} | armadura {me.armor:.0f} | RM {me.mr:.0f} | letalidade {me.lethality:.0f} | "
                 f"pen. armadura {me.armor_pen_pct:.0%} | ouro {rec.gold:.0f}")
        L.append(f"Combo simulado: {rec.combo_desc}")
        p = rec.profile
        L.append(f"Dano inimigo estimado: {p['fisico']:.0%} físico / {p['magico']:.0%} mágico "
                 f"(letalidade média {p['letalidade']:.0f}, pen. armadura {p['pen_armadura']:.0%}, pen. mágica {p['pen_magica_pct']:.0%})")
        L.append("Peso dos alvos: " + ", ".join(f"{n} {v:.0%}" for n, v in sorted(rec.weights.items(), key=lambda x: -x[1])))
        L.append("")
        L.append("Inimigos agora (vida cheia assumida):")
        for k, st in est.items():
            L.append(f"  {names[k]:<14} nv {st.level:<2} vida {st.hp:5.0f}  armadura {st.armor:4.0f}  RM {st.mr:4.0f}  "
                     f"→ seu combo tira {rec.ranking[0].dmg_before[k]:.0%}")
        L.append("")
        for i, c in enumerate(rec.ranking, 1):
            L.append(f"{i}. {c.item.name} — {c.cost} de ouro (faltam {c.remaining}) — pontuação {c.per_1000:.2f} por 1000 de ouro")
            L.append("   dano do combo: " + " | ".join(
                f"{names[k]} {c.dmg_before[k]:.0%}→{c.dmg_after[k]:.0%}" for k in est))
            L.append(f"   dano do combo ponderado: {c.dmg_gain:+.1f}% | vida efetiva {c.ehp_before:.0f}→{c.ehp_after:.0f} "
                     f"({c.ehp_gain:+.1f}%)" + (f" | anticura {c.antiheal:+.1f}%" if c.antiheal else ""))
            st = ", ".join(f"{k} {v:g}" for k, v in c.item.stats.items())
            L.append(f"   atributos: {st}")
            why = {"dano": "mais dano no combo contra os alvos prioritários",
                   "defesa": "aguenta mais contra o perfil de dano inimigo",
                   "anticura": "corta a cura dos inimigos"}[c.main_reason]
            L.append(f"   por quê: {why}")
            for n in c.notes:
                L.append(f"   obs.: {n}")
        L.append("")
        if rec.buy_now:
            L.append(f"Com {rec.gold:.0f} de ouro agora: compre {', '.join(rec.buy_now)}")
        else:
            L.append(f"Com {rec.gold:.0f} de ouro ainda não dá pra nenhum componente de {rec.ranking[0].item.name}")
        L.append("Limites: inimigo com vida cheia, sem runas inimigas, sem escudos/curas deles; "
                 "aceleração e velocidade de ataque não contam no combo único.")
        return "\n".join(L)

    # ---------------- fim de jogo ----------------
    def summary(self, built: list[tuple[float, int]]) -> str:
        """Compara cada item completo comprado com a recomendação anterior à compra."""
        L = ["RESUMO DA BUILD (feita x recomendada)"]
        if not built:
            L.append("  nenhum item completo comprado.")
        hits = 0
        for t, iid in built:
            it = self.idb.get(iid)
            prev = [r for r in self.history if r.game_time < t]
            mm = f"{int(t) // 60:02d}:{int(t) % 60:02d}"
            if not prev:
                L.append(f"  {mm} {it.name if it else iid}: sem recomendação antes")
                continue
            r = prev[-1]
            top = next((c for c in r.ranking if c.item.id == r.chosen_id), r.ranking[0])
            if top.item.id == iid:
                hits += 1
                L.append(f"  {mm} {top.item.name}: seguiu a recomendação ({r.chosen_by}, {top.per_1000:.2f}/1000g, motivo: {top.main_reason})")
            else:
                mine = next((c for c in r.ranking if c.item.id == iid), None)
                diff = f"o seu tinha {mine.per_1000:.2f}" if mine else "o seu não estava no top"
                L.append(f"  {mm} fez {it.name if it else iid}; recomendado ({r.chosen_by}) era {top.item.name} "
                         f"({top.per_1000:.2f}/1000g, {diff}). Motivo do recomendado: {top.main_reason}, "
                         f"dano {top.dmg_gain:+.1f}%, vida efetiva {top.ehp_gain:+.1f}%")
        if built:
            L.append(f"  Seguiu {hits} de {len(built)} recomendações.")
        return "\n".join(L)
