"""Data Dragon: baixa e guarda em cache os dados de campeões e itens do patch atual."""
from __future__ import annotations

import json
import re
from pathlib import Path

import httpx

from .config import ROOT

BASE = "https://ddragon.leagueoflegends.com"
CACHE = ROOT / "data" / "ddragon"
LANGS = ("pt_BR", "en_US")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def clean_text(html: str, limit: int | None = None) -> str:
    """Descrição do Data Dragon sem tags: '<attention>55</attention> de Dano<br>' -> '55 de Dano'."""
    s = re.sub(r"<br\s*/?>", " ", str(html or ""))
    s = re.sub(r"<li>", " ", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    if limit and len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0] + "..."
    return s


class DataDragon:
    def __init__(self, cache_dir: Path = CACHE):
        self.cache_dir = cache_dir
        self.version: str | None = None
        self.champions: dict[str, dict] = {}   # id ("MissFortune") -> dados (pt_BR) + name_en
        self.items: dict[int, dict] = {}       # id -> dados (pt_BR) + name_en
        self._champ_norm: dict[str, str] = {}
        self._item_norm: dict[str, int] = {}
        self._kits: dict[str, dict] = {}

    # ---------- carga ----------
    def load(self) -> "DataDragon":
        version = None
        try:
            r = httpx.get(f"{BASE}/api/versions.json", timeout=10)
            r.raise_for_status()
            version = r.json()[0]
        except Exception as e:  # sem internet: usa o último cache
            cached = sorted(p.name for p in self.cache_dir.glob("*") if p.is_dir())
            if not cached:
                raise RuntimeError(f"Sem acesso ao Data Dragon e sem cache: {e}") from e
            version = cached[-1]
            print(f"[ddragon] offline, usando cache {version}")
        self.version = version
        champs = {lang: self._get(version, lang, "champion.json") for lang in LANGS}
        items = {lang: self._get(version, lang, "item.json") for lang in LANGS}

        for cid, c in champs["pt_BR"]["data"].items():
            c = dict(c)
            c["name_en"] = champs["en_US"]["data"].get(cid, {}).get("name", c["name"])
            self.champions[cid] = c
            for key in (cid, c["name"], c["name_en"], c.get("key", "")):
                self._champ_norm[_norm(key)] = cid

        for iid, it in items["pt_BR"]["data"].items():
            it = dict(it)
            it["id"] = int(iid)
            it["name_en"] = items["en_US"]["data"].get(iid, {}).get("name", it["name"])
            self.items[int(iid)] = it
            # Só itens do Summoner's Rift (mapa 11) entram no índice por nome.
            if it.get("maps", {}).get("11", True):
                for key in (it["name"], it["name_en"]):
                    self._item_norm.setdefault(_norm(key), int(iid))
        return self

    def _get(self, version: str, lang: str, name: str) -> dict:
        path = self.cache_dir / version / lang / name
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        r = httpx.get(f"{BASE}/cdn/{version}/data/{lang}/{name}", timeout=30)
        r.raise_for_status()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(r.text, encoding="utf-8")
        return r.json()

    # ---------- ids numéricos (cliente do LoL / API da Riot) ----------
    def champ_by_key(self, key) -> str | None:
        """Id numérico do campeão (ex.: 238) -> id do Data Dragon ("Zed")."""
        k = str(key)
        return next((cid for cid, c in self.champions.items() if c.get("key") == k), None)

    def rune_name(self, rid) -> str:
        if not hasattr(self, "_runes"):
            self._runes = {}
            try:
                for tree in self._get(self.version, "pt_BR", "runesReforged.json"):
                    self._runes[tree["id"]] = tree["name"]
                    for slot in tree["slots"]:
                        for r in slot["runes"]:
                            self._runes[r["id"]] = r["name"]
            except Exception:
                pass
        return self._runes.get(int(rid), str(rid))

    def spell_name(self, key) -> str:
        if not hasattr(self, "_spells"):
            self._spells = {}
            try:
                for sp in self._get(self.version, "pt_BR", "summoner.json")["data"].values():
                    self._spells[int(sp["key"])] = sp["name"]
            except Exception:
                pass
        return self._spells.get(int(key), str(key))

    # ---------- textos do patch (pro Sonnet) ----------
    def item_text(self, iid: int, limit: int = 420) -> str:
        """Atributos e passivas do item como estão no patch atual, em português."""
        it = self.item(iid)
        if not it:
            return ""
        return clean_text(it.get("description", ""), limit)

    def champion_kit(self, cid: str | None, limit: int = 300) -> dict:
        """Passiva e habilidades (nome, descrição, recarga e custo por nível) do patch atual."""
        if not cid or cid not in self.champions:
            return {}
        if cid not in self._kits:
            try:
                data = self._get(self.version, "pt_BR", f"champion/{cid}.json")["data"][cid]
            except Exception:
                return {}
            kit = {"passiva": {"nome": data["passive"]["name"],
                               "descricao": clean_text(data["passive"]["description"], limit)}}
            for key, sp in zip("QWER", data.get("spells", [])):
                kit[key] = {"nome": sp["name"], "descricao": clean_text(sp["description"], limit),
                            "recarga_s": sp.get("cooldownBurn"), "custo": sp.get("costBurn")}
            self._kits[cid] = kit
        return self._kits[cid]

    # ---------- consultas ----------
    def champ_id(self, raw_or_name: str | None) -> str | None:
        """Aceita 'game_character_displayname_MissFortune', 'Miss Fortune', 'MissFortune'."""
        if not raw_or_name:
            return None
        s = raw_or_name.replace("game_character_displayname_", "")
        return self._champ_norm.get(_norm(s))

    def champ_name(self, cid: str | None) -> str:
        if cid and cid in self.champions:
            return self.champions[cid]["name"]
        return cid or "?"

    def item(self, iid: int) -> dict | None:
        return self.items.get(int(iid))

    def item_id_by_name(self, name: str) -> int | None:
        return self._item_norm.get(_norm(name))

    def item_cost(self, iid: int) -> int:
        it = self.item(iid)
        return int(it["gold"]["total"]) if it else 0

    def is_boots(self, iid: int) -> bool:
        it = self.item(iid)
        return bool(it) and "Boots" in it.get("tags", [])

    def is_completed(self, iid: int) -> bool:
        """Item 'fechado': não evolui pra nada no SR e custa bastante (lendário),
        ou bota de tier 2. Consumíveis, sentinelas e iniciais não contam."""
        it = self.item(iid)
        if not it:
            return False
        tags = it.get("tags", [])
        if "Consumable" in tags or "Trinket" in tags:
            return False
        total = int(it["gold"]["total"])
        into = [i for i in it.get("into", []) if self._on_sr(i)]
        if self.is_boots(iid):
            return total >= 900  # tier 2+ (tier 3 é upgrade por missão, não "fecha" a bota)
        return total >= 2000 and not into

    def _on_sr(self, iid) -> bool:
        it = self.items.get(int(iid))
        return bool(it) and it.get("maps", {}).get("11", True) and it.get("gold", {}).get("purchasable", True)
