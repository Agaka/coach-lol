"""Gera uma partida sintética (Zed mid vs Ahri) no mesmo formato da Live API,
pra testar regras e voz sem o jogo aberto. Os itens vêm do Data Dragon atual."""
from __future__ import annotations

import gzip
import json
from pathlib import Path

from .ddragon import DataDragon

END = 24 * 60


def _pick(dd: DataDragon, names: list[str], tag: str | None = None, completed: bool = False) -> int:
    for n in names:
        iid = dd.item_id_by_name(n)
        if iid:
            return iid
    for iid, it in sorted(dd.items.items()):
        if it.get("maps", {}).get("11") and it["gold"].get("purchasable") and (not tag or tag in it.get("tags", [])):
            if not completed or dd.is_completed(iid):
                return iid
    raise RuntimeError(f"item não encontrado: {names}")


def _hp(dd: DataDragon, cid: str, lvl: int) -> float:
    st = dd.champions[cid]["stats"]
    g = lvl - 1
    return round(st["hp"] + st["hpperlevel"] * g * (0.7025 + 0.0175 * g), 1)


# nível por tempo (segundos) — Ahri pega 2 e 6 antes, Zed pega 3 antes
ZED_LVL = [(0, 1), (110, 2), (160, 3), (210, 4), (270, 5), (350, 6), (420, 7), (500, 8), (580, 9),
           (660, 10), (740, 11), (830, 12), (920, 13), (1010, 14), (1100, 15), (1200, 16), (1300, 17), (1400, 18)]
AHRI_LVL = [(0, 1), (100, 2), (170, 3), (215, 4), (275, 5), (330, 6), (430, 7), (520, 8), (600, 9),
            (690, 10), (780, 11), (870, 12), (960, 13), (1060, 14), (1160, 15), (1260, 16), (1360, 17), (1460, 18)]


def _at(table, t):
    v = table[0][1]
    for tt, vv in table:
        if t >= tt:
            v = vv
    return v


def write_sample(path: Path) -> Path:
    dd = DataDragon().load()
    I = {
        "dblade": _pick(dd, ["Doran's Blade"]),
        "dring": _pick(dd, ["Doran's Ring"]),
        "dshield": _pick(dd, ["Doran's Shield"]),
        "pot": _pick(dd, ["Health Potion"]),
        "boots": _pick(dd, ["Boots"]),
        "lsword": _pick(dd, ["Long Sword"]),
        "dirk": _pick(dd, ["Serrated Dirk"]),
        "youmuu": _pick(dd, ["Youmuu's Ghostblade", "Opportunity", "Profane Hydra"], "Damage", True),
        "ionian": _pick(dd, ["Ionian Boots of Lucidity"], "Boots", True),
        "tome": _pick(dd, ["Amplifying Tome"]),
        "luden": _pick(dd, ["Luden's Companion", "Luden's Tempest", "Malignance"], "SpellDamage", True),
        "sorc": _pick(dd, ["Sorcerer's Shoes"], "Boots", True),
        "ad2": _pick(dd, ["The Collector", "Edge of Night", "Serylda's Grudge"], "Damage", True),
        "tank": _pick(dd, ["Sunfire Aegis", "Heartsteel", "Thornmail"], "Health", True),
        "crit": _pick(dd, ["Infinity Edge", "The Collector", "Kraken Slayer"], "CriticalStrike", True),
    }
    zed_items = [(0, ["dblade", "pot"]), (395, ["dblade", "lsword", "lsword"]),
                 (645, ["dblade", "youmuu"]), (1000, ["dblade", "youmuu", "ionian"]),
                 (1250, ["youmuu", "ionian", "ad2"])]
    ahri_items = [(0, ["dring", "pot", "pot"]), (480, ["dring", "tome", "boots"]),
                  (570, ["dring", "luden", "boots"]), (1050, ["dring", "luden", "sorc"])]
    generic = {
        "Warwick": [(0, []), (600, ["boots"]), (900, ["tank", "boots"])],
        "Garen": [(0, ["dshield"]), (700, ["dshield", "tank"])],
        "MissFortune": [(0, ["dblade"]), (720, ["dblade", "crit"]), (1300, ["crit", "ad2"])],
        "Leona": [(0, []), (800, ["tank"])],
        "LeeSin": [(0, []), (700, ["boots"]), (1000, ["ad2"])],
        "Darius": [(0, ["dshield"]), (800, ["dshield", "tank"])],
        "Jinx": [(0, ["dblade"]), (900, ["dblade", "crit"])],
        "Thresh": [(0, []), (900, ["tank"])],
    }
    roster = [
        # (riotId, campeão, time, posição)
        ("Eu#BR1", "Zed", "ORDER", "MIDDLE"),
        ("Lobo#BR1", "Warwick", "ORDER", "JUNGLE"),
        ("Demacia#BR1", "Garen", "ORDER", "TOP"),
        ("Bala#BR1", "MissFortune", "ORDER", "BOTTOM"),
        ("Sol#BR1", "Leona", "ORDER", "UTILITY"),
        ("Raposa#BR1", "Ahri", "CHAOS", "MIDDLE"),
        ("Cego#BR1", "LeeSin", "CHAOS", "JUNGLE"),
        ("Noxus#BR1", "Darius", "CHAOS", "TOP"),
        ("Foguete#BR1", "Jinx", "CHAOS", "BOTTOM"),
        ("Gancho#BR1", "Thresh", "CHAOS", "UTILITY"),
    ]
    rid = {c: r for r, c, _, _ in roster}

    # eventos: (tempo, nome, campos)
    raw_events = [
        (0.05, "GameStart", {}),
        (65, "MinionsSpawning", {}),
        (320, "DragonKill", {"DragonType": "Fire", "Stolen": "False", "KillerName": rid["Warwick"]}),
        (390, "ChampionKill", {"KillerName": rid["Ahri"], "VictimName": rid["Zed"]}),
        (510, "HordeKill", {"KillerName": rid["Warwick"]}),
        (512, "HordeKill", {"KillerName": rid["Warwick"]}),
        (515, "HordeKill", {"KillerName": rid["Warwick"]}),
        (715, "ChampionKill", {"KillerName": rid["MissFortune"], "VictimName": rid["Jinx"]}),
        (720, "ChampionKill", {"KillerName": rid["Zed"], "VictimName": rid["Ahri"]}),
        (750, "DragonKill", {"DragonType": "Earth", "Stolen": "False", "KillerName": rid["Warwick"]}),
        (930, "HeraldKill", {"KillerName": rid["LeeSin"]}),
        (1090, "ChampionKill", {"KillerName": rid["Darius"], "VictimName": rid["Zed"]}),
        (1255, "ChampionKill", {"KillerName": rid["Zed"], "VictimName": rid["Ahri"]}),
        (1257, "ChampionKill", {"KillerName": rid["Garen"], "VictimName": rid["Darius"]}),
        (1259, "ChampionKill", {"KillerName": rid["MissFortune"], "VictimName": rid["Jinx"]}),
        (1262, "ChampionKill", {"KillerName": rid["Leona"], "VictimName": rid["Thresh"]}),
        (1265, "ChampionKill", {"KillerName": rid["Warwick"], "VictimName": rid["LeeSin"]}),
        (1265.5, "Ace", {"Acer": rid["Warwick"], "AcingTeam": "ORDER"}),
        (1290, "BaronKill", {"Stolen": "False", "KillerName": rid["Warwick"]}),
        (1400, "InhibKilled", {"KillerName": rid["Garen"], "InhibKilled": "Barracks_T2_L1"}),
        (END, "GameEnd", {"Result": "Win"}),
    ]
    death_until: dict[str, float] = {}
    for t, n, f in raw_events:
        if n == "ChampionKill":
            v = next(c for r, c, _, _ in roster if r == f["VictimName"])
            death_until[(v, t)] = t + (20 if t < 900 else 35)

    def dead(c, t):
        for (v, kt), until in death_until.items():
            if v == c and kt <= t < until:
                return True, until - t
        return False, 0.0

    def zed_hp_frac(t):
        if 235 <= t < 250: return 0.45        # troca ruim
        if 250 <= t < 262: return 0.26        # vida baixa
        if 262 <= t < 300: return 0.55
        if 380 <= t < 390: return 0.12        # vai morrer
        if 1080 <= t < 1090: return 0.10
        if 1240 <= t < 1255: return 0.40
        return 1.0

    # ouro do Zed: renda constante, gastos nas compras
    purchases = {0: 500 - 50, 395: 875, 645: 1300 + 400, 1000: 950, 1250: 2900}

    out = []
    gold = 500.0
    for t in range(0, END + 1):
        t = float(t)
        income = 0 if t < 65 else 4.6
        gold += income
        for pt, cost in purchases.items():
            if t == pt:
                gold -= cost
                if pt == 0:
                    gold = 50.0
        gold = max(gold, 0)
        players = []
        for r, c, team, pos in roster:
            if c == "Zed":
                inv, lvl = _at(zed_items, t), _at(ZED_LVL, t)
            elif c == "Ahri":
                inv, lvl = _at(ahri_items, t), _at(AHRI_LVL, t)
            else:
                inv, lvl = _at(generic[c], t), min(18, 1 + int(t // 75))
            k = sum(1 for _t, n, f in raw_events if _t <= t and n == "ChampionKill" and f["KillerName"] == r)
            d = sum(1 for _t, n, f in raw_events if _t <= t and n == "ChampionKill" and f["VictimName"] == r)
            is_dead, resp = dead(c, t)
            players.append({
                "championName": dd.champions[c]["name"],
                "rawChampionName": f"game_character_displayname_{c}",
                "isBot": False, "isDead": is_dead, "respawnTimer": round(resp, 1),
                "level": lvl, "position": pos, "team": team,
                "riotId": r, "riotIdGameName": r.split("#")[0], "riotIdTagLine": "BR1", "summonerName": r,
                "scores": {"kills": k, "deaths": d, "assists": 0,
                           "creepScore": int(max(0, t - 90) / 60 * (7.2 if c == "Zed" else 6.5)) if pos != "UTILITY" else 10,
                           "wardScore": t / 120},
                "summonerSpells": {
                    "summonerSpellOne": {"displayName": "Flash", "rawDisplayName": "GeneratedTip_SummonerSpell_SummonerFlash_DisplayName"},
                    "summonerSpellTwo": ({"displayName": "Golpear", "rawDisplayName": "GeneratedTip_SummonerSpell_SummonerSmite_DisplayName"}
                                         if pos == "JUNGLE" else
                                         {"displayName": "Incendiar", "rawDisplayName": "GeneratedTip_SummonerSpell_SummonerDot_DisplayName"}),
                },
                "items": [{"itemID": I[key], "count": 1, "slot": i, "displayName": dd.items[I[key]]["name"],
                           "price": dd.item_cost(I[key]), "canUse": False, "consumable": False}
                          for i, key in enumerate(inv)],
            })
        zlvl = _at(ZED_LVL, t)
        max_hp = _hp(dd, "Zed", zlvl)
        zdead, _ = dead("Zed", t)
        hp = 0.0 if zdead else round(max_hp * zed_hp_frac(t), 1)
        events = [{"EventID": i, "EventName": n, "EventTime": et, **f}
                  for i, (et, n, f) in enumerate(raw_events) if et <= t]
        q, w, e = min(5, (zlvl + 2) // 3), min(5, zlvl // 3), min(5, max(1, (zlvl + 1) // 3))
        data = {
            "activePlayer": {
                "riotId": "Eu#BR1", "riotIdGameName": "Eu", "riotIdTagLine": "BR1", "summonerName": "Eu#BR1",
                "level": zlvl, "currentGold": round(gold, 1),
                "abilities": {"Q": {"abilityLevel": q}, "W": {"abilityLevel": w}, "E": {"abilityLevel": e},
                              "R": {"abilityLevel": 0 if zlvl < 6 else 1 if zlvl < 11 else 2 if zlvl < 16 else 3}},
                "championStats": {"currentHealth": hp, "maxHealth": max_hp, "attackDamage": 63 + 3.4 * zlvl,
                                  "armor": 32 + 4.7 * zlvl, "magicResist": 32 + 2 * zlvl,
                                  "resourceType": "ENERGY", "resourceValue": 200, "resourceMax": 200},
            },
            "allPlayers": players,
            "events": {"Events": events},
            "gameData": {"gameMode": "CLASSIC", "gameTime": t, "mapName": "Map11", "mapNumber": 11},
        }
        out.append({"t": t, "data": data})

    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for s in out:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    return path
