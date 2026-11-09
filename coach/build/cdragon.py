"""CommunityDragon: atributos estruturados e valores de passivas dos itens, e dados
das habilidades dos campeões, no patch do Data Dragon. Fica em cache reduzido."""
from __future__ import annotations

import json
from pathlib import Path

import httpx

from ..config import ROOT

BASE = "https://raw.communitydragon.org"
CACHE = ROOT / "data" / "cdragon"

# Campos numéricos de item no bin -> nome interno
ITEM_STATS = {
    "mFlatPhysicalDamageMod": "ad",
    "mFlatMagicDamageMod": "ap",
    "mFlatHPPoolMod": "hp",
    "mFlatArmorMod": "armor",
    "mFlatSpellBlockMod": "mr",
    "mAbilityHasteMod": "ah",
    "PhysicalLethality": "lethality",
    "mFlatArmorPenetrationMod": "lethality",
    "mPercentArmorPenetrationMod": "armor_pen_pct",
    "mFlatMagicPenetrationMod": "magic_pen",
    "mPercentMagicPenetrationMod": "magic_pen_pct",
    "mPercentAttackSpeedMod": "as_pct",
    "mFlatCritChanceMod": "crit",
    "mFlatCritDamageMod": "crit_dmg",
    "mPercentLifeStealMod": "lifesteal",
    "PercentOmnivampMod": "omnivamp",
    "mFlatMovementSpeedMod": "ms",
    "mPercentMovementSpeedMod": "ms_pct",
    "flatMPPoolMod": "mana",
}


class CommunityDragon:
    def __init__(self, ddragon_version: str, cache_dir: Path = CACHE):
        self.patch = ".".join(ddragon_version.split(".")[:2])   # "16.21.1" -> "16.21"
        self.dir = cache_dir / self.patch
        self.items: dict[int, dict] = {}
        self.groups: dict[str, int] = {}     # grupo -> máximo que pode ter
        self._champs: dict[str, dict] = {}

    def _download(self, path: str) -> bytes:
        for ver in (self.patch, "latest"):
            r = httpx.get(f"{BASE}/{ver}/{path}", timeout=60)
            if r.status_code == 200:
                return r.content
        r.raise_for_status()
        return b""

    def load(self) -> "CommunityDragon":
        cached = self.dir / "items_v2.json"
        if cached.exists():
            data = json.loads(cached.read_text(encoding="utf-8"))
        else:
            raw = json.loads(self._download("game/items.cdtb.bin.json"))
            data = {"items": {}, "groups": {}}
            for k, v in raw.items():
                if not isinstance(v, dict):
                    continue
                if v.get("__type") == "ItemGroup":
                    data["groups"][k] = int(v.get("mMaxGroupOwnable", 0) or 0)
                elif k.startswith("Items/") and "itemID" in v:
                    stats: dict[str, float] = {}
                    for f, name in ITEM_STATS.items():
                        if isinstance(v.get(f), (int, float)):
                            stats[name] = stats.get(name, 0.0) + float(v[f])
                    values: dict[str, float] = {}
                    for d in v.get("mDataValues") or []:
                        if "mName" in d:
                            values.setdefault(d["mName"], float(d.get("mValue", 0.0)))
                    data["items"][str(v["itemID"])] = {
                        "stats": stats,
                        "values": values,
                        "groups": [g for g in v.get("mItemGroups", []) if g != "Items/ItemGroups/Default"],
                        "in_store": bool((v.get("mItemDataAvailability") or {}).get("mInStore", True)),
                    }
            self.dir.mkdir(parents=True, exist_ok=True)
            cached.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self.items = {int(k): v for k, v in data["items"].items()}
        self.groups = data["groups"]
        return self

    def champion(self, cid: str) -> dict:
        """Spells do campeão: {nome_curto: {"values": {nome: [por rank]}, "calcs": {...}}}."""
        if cid in self._champs:
            return self._champs[cid]
        path = self.dir / "champions" / f"{cid.lower()}.json"
        if path.exists():
            spells = json.loads(path.read_text(encoding="utf-8"))
        else:
            raw = json.loads(self._download(f"game/data/characters/{cid.lower()}/{cid.lower()}.bin.json"))
            spells = {}
            for k, v in raw.items():
                if not isinstance(v, dict) or v.get("__type") != "SpellObject":
                    continue
                sp = v.get("mSpell") or {}
                values = {d["name"]: d["values"] for d in sp.get("DataValues") or [] if d.get("values")}
                calcs = sp.get("mSpellCalculations") or {}
                if values or calcs:
                    spells[k.split("/")[-1]] = {"values": values, "calcs": calcs}
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(spells, ensure_ascii=False), encoding="utf-8")
        self._champs[cid] = spells
        return spells
