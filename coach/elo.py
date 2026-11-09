"""Fase 4: estatísticas de elo alto a partir de partidas ranqueadas baixadas da API web da Riot.

Endpoints (developer.riotgames.com; formato conferido no schema OpenAPI gerado da documentação oficial):
- league-v4  (host da plataforma, ex. br1):  /lol/league/v4/{challenger,grandmaster,master}leagues/by-queue/RANKED_SOLO_5x5
             -> {"entries": [{"puuid", "leaguePoints", ...}]}
- match-v5   (host regional, ex. americas): /lol/match/v5/matches/by-puuid/{puuid}/ids?queue=420&type=ranked&count=N
             /lol/match/v5/matches/{id}  -> info.participants[]: championName, teamPosition, teamId, win, participantId
             /lol/match/v5/matches/{id}/timeline -> info.frames[]: timestamp (ms), participantFrames{"1".."10"}:
                                                    totalGold, currentGold, level, minionsKilled, jungleMinionsKilled
Chave no cabeçalho X-Riot-Token, lida de RIOT_API_KEY (chaves.env). Chave de desenvolvimento expira em 24 h.

Cada partida vira um resumo pequeno em data/elo_alto/<plataforma>/partidas/<id>.json; as estatísticas são
recalculadas a partir deles. Ouro comparado = ouro gasto (totalGold - currentGold), que é o que a Live API
deixa ver do oponente (valor dos itens)."""
from __future__ import annotations

import json
import os
import random
import time
from collections import defaultdict, deque
from pathlib import Path

import httpx

from .config import ROOT

TIERS = ("challenger", "grandmaster", "master")
ROLES = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")


class RiotError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(f"HTTP {status}: {msg}")
        self.status = status


class RiotClient:
    """GET com limite de requisições (lido do cabeçalho X-App-Rate-Limit; padrão da chave de desenvolvimento)."""

    def __init__(self, key: str, platform: str, region: str, limits=((20, 1), (100, 120)), transport=None):
        self.key, self.platform, self.region = key, platform.lower(), region.lower()
        self.limits = [(int(n), float(w)) for n, w in limits]
        self.sent: deque[float] = deque()
        self.http = httpx.Client(timeout=20, transport=transport)
        self.requests = 0

    def _wait(self) -> None:
        while True:
            now = time.time()
            longest = max(w for _, w in self.limits)
            while self.sent and now - self.sent[0] > longest:
                self.sent.popleft()
            delay = 0.0
            for n, w in self.limits:
                recent = [t for t in self.sent if now - t < w]
                if len(recent) >= n:
                    delay = max(delay, w - (now - recent[-n]) + 0.05)
            if delay <= 0:
                return
            time.sleep(delay)

    def get(self, host: str, path: str, params: dict | None = None):
        for attempt in range(5):
            self._wait()
            self.sent.append(time.time())
            self.requests += 1
            try:
                r = self.http.get(f"https://{host}.api.riotgames.com{path}", params=params,
                                  headers={"X-Riot-Token": self.key})
            except httpx.HTTPError as e:
                if attempt == 4:
                    raise RiotError(0, str(e))
                time.sleep(2 * (attempt + 1))
                continue
            lim = r.headers.get("X-App-Rate-Limit")
            if lim:
                try:
                    self.limits = [(int(a), float(b)) for a, b in (x.split(":") for x in lim.split(","))]
                except ValueError:
                    pass
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", 10)))
                continue
            if r.status_code >= 500:
                time.sleep(2 * (attempt + 1))
                continue
            raise RiotError(r.status_code, r.text[:200])
        raise RiotError(429, "limite de requisições estourado várias vezes")

    def ladder(self, tier: str) -> list[dict]:
        d = self.get(self.platform, f"/lol/league/v4/{tier}leagues/by-queue/RANKED_SOLO_5x5")
        return list(d.get("entries") or [])

    def match_ids(self, puuid: str, count: int) -> list[str]:
        return self.get(self.region, f"/lol/match/v5/matches/by-puuid/{puuid}/ids",
                        {"queue": 420, "type": "ranked", "start": 0, "count": count})

    def match(self, mid: str) -> dict:
        return self.get(self.region, f"/lol/match/v5/matches/{mid}")

    def timeline(self, mid: str) -> dict:
        return self.get(self.region, f"/lol/match/v5/matches/{mid}/timeline")


# ---------------- resumo de uma partida ----------------
def _frame_at(frames: list[dict], minute: int) -> dict | None:
    target = minute * 60_000
    for f in frames:
        if f.get("timestamp", 0) >= target:
            return f
    return None


def reduce_match(match: dict, timeline: dict, minutes=(10, 15)) -> dict | None:
    """Só o que as estatísticas usam. None para remake, partida curta ou posições incompletas."""
    info = match.get("info") or {}
    parts = info.get("participants") or []
    if len(parts) != 10 or float(info.get("gameDuration", 0)) < max(minutes) * 60 + 60:
        return None
    if sorted(p.get("teamPosition") or "" for p in parts) != sorted(ROLES * 2):
        return None
    frames = (timeline.get("info") or {}).get("frames") or []
    at = {m: _frame_at(frames, m) for m in minutes}
    if any(f is None for f in at.values()):
        return None
    out = []
    for p in parts:
        pid = str(p["participantId"])
        snap = {}
        for m, f in at.items():
            pf = (f.get("participantFrames") or {}).get(pid) or {}
            snap[str(m)] = {"gasto": int(pf.get("totalGold", 0)) - int(pf.get("currentGold", 0)),
                            "nivel": int(pf.get("level", 1)),
                            "cs": int(pf.get("minionsKilled", 0)) + int(pf.get("jungleMinionsKilled", 0))}
        out.append({"campeao": p.get("championName"), "rota": p.get("teamPosition"), "time": int(p.get("teamId", 0)),
                    "vitoria": bool(p.get("win")), "min": snap, "itens": _final_items(p), **_loadout(p)})
    version = str(info.get("gameVersion", ""))
    return {"id": (match.get("metadata") or {}).get("matchId"), "patch": ".".join(version.split(".")[:2]),
            "duracao_s": int(info.get("gameDuration", 0)), "jogadores": out}


def _loadout(p: dict) -> dict:
    """Runas e feitiços do jogador (match-v5: perks.styles[primaryStyle|subStyle].selections, statPerks)."""
    perks = p.get("perks") or {}
    styles = {st.get("description"): st for st in perks.get("styles") or []}
    prim, sub = styles.get("primaryStyle") or {}, styles.get("subStyle") or {}
    stat = perks.get("statPerks") or {}
    return {"runas": {"principal": prim.get("style"), "secundaria": sub.get("style"),
                      "selecoes": [x.get("perk") for x in (prim.get("selections") or []) + (sub.get("selections") or [])],
                      "fragmentos": [stat.get("offense"), stat.get("flex"), stat.get("defense")]},
            "feiticos": sorted(x for x in (p.get("summoner1Id"), p.get("summoner2Id")) if x)}


def _final_items(p: dict) -> list[int]:
    """Inventário no fim da partida (item0..item6 do match-v5), sem espaços vazios."""
    return [int(p.get(f"item{i}") or 0) for i in range(7) if p.get(f"item{i}")]


def enrich_items(client: RiotClient, folder: Path, log=print) -> int:
    """Partidas baixadas antes de guardar itens: busca só o match (sem timeline) e completa."""
    todo = []
    for f in folder.glob("*.json"):
        try:
            m = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if any("itens" not in j or "runas" not in j for j in m.get("jogadores", [])):
            todo.append((f, m))
    if not todo:
        return 0
    log(f"Completando itens e runas de {len(todo)} partidas já baixadas (~{len(todo) * 1.2 / 60:.0f} min)...")
    done = 0
    for f, m in todo:
        try:
            parts = (client.match(f.stem).get("info") or {}).get("participants") or []
        except RiotError as e:
            if e.status in (401, 403):
                log(_explain(e))
                break
            continue
        by = {(int(p.get("teamId", 0)), p.get("teamPosition")): p for p in parts}
        for j in m["jogadores"]:
            p = by.get((j["time"], j["rota"]))
            j["itens"] = _final_items(p) if p else []
            if p:
                j.update(_loadout(p))
        f.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
        done += 1
        if done % 100 == 0:
            log(f"  itens: {done}/{len(todo)}")
    return done


# ---------------- download ----------------
def download(cfg: dict, max_matches: int | None = None, transport=None, log=print) -> int:
    ec = cfg["estatisticas"]
    key = os.environ.get("RIOT_API_KEY", "")
    if not key:
        log("Sem RIOT_API_KEY. Pegue uma chave em developer.riotgames.com e cole no chaves.env (RIOT_API_KEY=...).")
        return 0
    client = RiotClient(key, ec["plataforma"], ec["regiao"], transport=transport)
    folder = data_dir(cfg) / "partidas"
    folder.mkdir(parents=True, exist_ok=True)
    have = {p.stem for p in folder.glob("*.json")}
    skipped_file = folder.parent / "ignoradas.txt"
    skipped = set(skipped_file.read_text(encoding="utf-8").split()) if skipped_file.exists() else set()
    goal = int(ec["partidas_max"] if max_matches is None else max_matches)
    log(f"Já tenho {len(have)} partidas. Meta: {goal} novas de {', '.join(ec['elos'])} ({ec['plataforma']}).")
    enrich_items(client, folder, log)
    if goal <= 0:
        build_stats(cfg)
        log(f"Estatísticas atualizadas em {data_dir(cfg) / 'estatisticas.json'}")
        return 0

    puuids: list[str] = []
    try:
        for tier in ec["elos"]:
            entries = client.ladder(tier.lower())
            entries.sort(key=lambda e: -int(e.get("leaguePoints", 0)))
            puuids += [e["puuid"] for e in entries if e.get("puuid")]
            log(f"  {tier}: {len(entries)} jogadores")
    except RiotError as e:
        log(_explain(e))
        return 0
    random.Random(len(have)).shuffle(puuids)   # varia os jogadores a cada rodada

    new = 0
    seen = set(have) | skipped
    t0 = time.time()
    for i, puuid in enumerate(puuids):
        if new >= goal:
            break
        try:
            ids = client.match_ids(puuid, int(ec["partidas_por_jogador"]))
        except RiotError as e:
            if e.status in (401, 403):
                log(_explain(e))
                break
            continue
        for mid in ids:
            if mid in seen or new >= goal:
                continue
            seen.add(mid)
            try:
                red = reduce_match(client.match(mid), client.timeline(mid))
            except RiotError as e:
                if e.status in (401, 403):
                    log(_explain(e))
                    return new
                continue
            if red is None:
                with open(skipped_file, "a", encoding="utf-8") as f:
                    f.write(mid + "\n")
                continue
            (folder / f"{mid}.json").write_text(json.dumps(red, ensure_ascii=False), encoding="utf-8")
            new += 1
            if new % 25 == 0 or new == goal:
                rate = new / max(1.0, time.time() - t0)
                log(f"  {new}/{goal} partidas ({client.requests} requisições; faltam ~{(goal - new) / rate / 60:.0f} min)")
    build_stats(cfg)
    log(f"Pronto: {new} partidas novas, {len(have) + new} no total. Estatísticas em {data_dir(cfg) / 'estatisticas.json'}")
    return new


def _explain(e: RiotError) -> str:
    if e.status in (401, 403):
        return f"A Riot recusou a chave ({e}). Chave de desenvolvimento expira em 24 h: gere outra em developer.riotgames.com."
    return f"Erro na API da Riot: {e}"


def data_dir(cfg: dict) -> Path:
    return ROOT / "data" / "elo_alto" / cfg["estatisticas"]["plataforma"].lower()


# ---------------- agregação ----------------
def _k(champ: str) -> str:
    """Chave de campeão sem diferença de maiúsculas (FiddleSticks na Riot x Fiddlesticks no Data Dragon)."""
    return "".join(ch for ch in str(champ).lower() if ch.isalnum())


def _bucket(diff: float, edges: list[float]) -> int:
    return sum(diff >= e for e in edges)


def bucket_label(i: int, edges: list[float]) -> str:
    fmt = lambda v: f"{abs(int(v))}"
    if i == 0:
        return f"atrás mais de {fmt(edges[0])}"
    if i == len(edges):
        return f"à frente mais de {fmt(edges[-1])}"
    lo, hi = edges[i - 1], edges[i]
    if lo < 0 <= hi:
        return f"entre -{fmt(lo)} e +{fmt(hi)}"
    side = "à frente" if lo >= 0 else "atrás"
    return f"{side} {fmt(min(abs(lo), abs(hi)))} a {fmt(max(abs(lo), abs(hi)))}"


def build_stats(cfg: dict) -> dict:
    ec = cfg["estatisticas"]
    edges = [float(x) for x in ec["faixas_ouro_lane"]]
    tedges = [float(x) for x in ec["faixas_ouro_time"]]
    folder = data_dir(cfg) / "partidas"
    lane = defaultdict(lambda: {"n": 0, "v": 0, "faixas": [[0, 0] for _ in range(len(edges) + 1)]})
    items = defaultdict(lambda: defaultdict(lambda: [0, 0]))      # rota|campeão -> item -> [partidas, vitórias]
    pages = defaultdict(lambda: defaultdict(lambda: [0, 0]))      # rota|campeão -> página de runas -> [partidas, vitórias]
    spells = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    team = [[0, 0] for _ in range(len(tedges) + 1)]
    cs10 = defaultdict(list)
    patches = defaultdict(int)
    n = 0
    for f in folder.glob("*.json"):
        try:
            m = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            continue
        n += 1
        patches[m.get("patch", "?")] += 1
        by = {(p["time"], p["rota"]): p for p in m["jogadores"]}
        for p in m["jogadores"]:
            opp = by.get((300 - p["time"], p["rota"]))
            if not opp:
                continue
            diff = p["min"]["10"]["gasto"] - opp["min"]["10"]["gasto"]
            b = _bucket(diff, edges)
            me_c, op_c = _k(p["campeao"]), _k(opp["campeao"])
            for key in (f"{p['rota']}|{me_c}|{op_c}", f"{p['rota']}|{me_c}"):
                s = lane[key]
                s["n"] += 1
                s["v"] += p["vitoria"]
                s["faixas"][b][0] += 1
                s["faixas"][b][1] += p["vitoria"]
            cs10[f"{p['rota']}|{me_c}"].append(p["min"]["10"]["cs"])
            r = p.get("runas") or {}
            if r.get("selecoes"):
                page = json.dumps([r["principal"], r["secundaria"], r["selecoes"], r["fragmentos"]])
                pages[f"{p['rota']}|{me_c}"][page][0] += 1
                pages[f"{p['rota']}|{me_c}"][page][1] += p["vitoria"]
            if p.get("feiticos"):
                sp = json.dumps(p["feiticos"])
                spells[f"{p['rota']}|{me_c}"][sp][0] += 1
                spells[f"{p['rota']}|{me_c}"][sp][1] += p["vitoria"]
            for iid in set(p.get("itens") or []):
                items[f"{p['rota']}|{me_c}"][str(iid)][0] += 1
                items[f"{p['rota']}|{me_c}"][str(iid)][1] += p["vitoria"]
            cs10[p["rota"]].append(p["min"]["10"]["cs"])
        gold = defaultdict(int)
        win = {}
        for p in m["jogadores"]:
            gold[p["time"]] += p["min"]["15"]["gasto"]
            win[p["time"]] = p["vitoria"]
        tb = _bucket(gold[100] - gold[200], tedges)
        team[tb][0] += 1
        team[tb][1] += win.get(100, False)
        tb2 = _bucket(gold[200] - gold[100], tedges)
        team[tb2][0] += 1
        team[tb2][1] += win.get(200, False)
    cs = {k: {"n": len(v), "mediana": sorted(v)[len(v) // 2], "p75": sorted(v)[(len(v) * 3) // 4]} for k, v in cs10.items() if v}
    out = {"partidas": n, "patches": dict(patches), "plataforma": ec["plataforma"], "elos": ec["elos"],
           "faixas_ouro_lane": edges, "faixas_ouro_time": tedges, "lane": dict(lane), "time_15": team, "cs10": cs,
           "itens": {k: dict(v) for k, v in items.items()},
           "runas": {k: dict(v) for k, v in pages.items()},
           "feiticos": {k: dict(v) for k, v in spells.items()},
           "gerado_em": time.strftime("%Y-%m-%d %H:%M")}
    (data_dir(cfg) / "estatisticas.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


class EloStats:
    """Consulta as estatísticas pra dar contexto (Sonnet, log, resumo pós-partida)."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.min_n = int(cfg["estatisticas"].get("amostra_minima", 20))
        path = data_dir(cfg) / "estatisticas.json"
        self.data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    @property
    def available(self) -> bool:
        return bool(self.data and self.data.get("partidas"))

    def source(self) -> str:
        d = self.data
        top = ", ".join(f"{k} ({v})" for k, v in sorted(d["patches"].items(), key=lambda x: -x[1])[:2])
        return f"{d['partidas']} partidas ranqueadas {'/'.join(e.title() for e in d['elos'])} {d['plataforma'].upper()}, patches {top}"

    def _lane_entry(self, key: str) -> dict | None:
        s = (self.data or {}).get("lane", {}).get(key)
        if not s or s["n"] < self.min_n:
            return None
        edges = self.data["faixas_ouro_lane"]
        faixas = [{"faixa_ouro_aos_10": bucket_label(i, edges), "partidas": n, "winrate": round(v / n, 3)}
                  for i, (n, v) in enumerate(s["faixas"]) if n >= max(5, self.min_n // 4)]
        return {"partidas": s["n"], "winrate": round(s["v"] / s["n"], 3), "por_ouro_aos_10": faixas}

    def lane(self, role: str, champ: str | None, opp: str | None) -> dict:
        """Matchup exato se houver amostra; senão o campeão na rota."""
        if not self.available or not champ:
            return {}
        out = {}
        if opp:
            m = self._lane_entry(f"{role}|{_k(champ)}|{_k(opp)}")
            if m:
                out["matchup"] = {"contra": opp, **m}
        c = self._lane_entry(f"{role}|{_k(champ)}")
        if c:
            out["campeao_na_rota"] = c
        cs = self.data["cs10"].get(f"{role}|{_k(champ)}")
        if not cs or cs["n"] < self.min_n:          # pouca amostra do campeão: usa a rota toda
            cs = self.data["cs10"].get(role)
        if cs:
            out["farm_aos_10"] = {"mediana": cs["mediana"], "p75": cs["p75"], "partidas": cs["n"]}
        if out:
            out["fonte"] = self.source()
        return out

    def lane_at(self, role: str, champ: str | None, opp: str | None, diff10: float) -> str | None:
        """'à frente 500 a 1500 aos 10 min: 64% de vitória (212 partidas, matchup)'."""
        if not self.available or not champ:
            return None
        edges = self.data["faixas_ouro_lane"]
        b = _bucket(diff10, edges)
        for key, what in ((f"{role}|{_k(champ)}|{_k(opp or '')}", f"{champ} x {opp}"), (f"{role}|{_k(champ)}", f"{champ} {role}")):
            s = self.data["lane"].get(key)
            if s and s["faixas"][b][0] >= self.min_n:
                n, v = s["faixas"][b]
                return f"{bucket_label(b, edges)} de ouro aos 10 min: {v / n:.0%} de vitória ({n} partidas, {what})"
        return None

    def team_at(self, diff15: float) -> str | None:
        if not self.available:
            return None
        edges = self.data["faixas_ouro_time"]
        b = _bucket(diff15, edges)
        n, v = self.data["time_15"][b]
        if n < self.min_n:
            return None
        return f"time {bucket_label(b, edges)} de ouro aos 15 min: {v / n:.0%} de vitória ({n} casos)"

    # ---------------- meta ----------------
    def items_for(self, role: str, champ: str | None, dd, top: int = 6) -> list[dict]:
        """Itens completos mais feitos pelo campeão na rota (uso e vitória com o item)."""
        if not self.available or not champ:
            return []
        key = f"{role}|{_k(champ)}"
        games = (self.data.get("lane", {}).get(key) or {}).get("n", 0)
        raw = (self.data.get("itens") or {}).get(key) or {}
        if games < self.min_n or not raw:
            return []
        rows = []
        for iid, (n, v) in raw.items():
            if dd.is_completed(int(iid)) and n >= 5:
                it = dd.item(int(iid))
                rows.append({"item": it["name"] if it else iid, "uso": round(n / games, 2), "partidas": n,
                             "winrate": round(v / n, 3), "bota": dd.is_boots(int(iid))})
        rows.sort(key=lambda r: -r["partidas"])
        return rows[:top]

    def meta_role(self, role: str, dd, top: int = 6) -> list[dict]:
        """Campeões mais escolhidos na rota, com vitória."""
        if not self.available:
            return []
        total = self.data["partidas"]
        rows = []
        for key, st in self.data.get("lane", {}).items():
            parts = key.split("|")
            if len(parts) == 2 and parts[0] == role and st["n"] >= self.min_n:
                cid = dd.champ_id(parts[1])
                rows.append({"campeao": dd.champ_name(cid) if cid else parts[1], "partidas": st["n"],
                             "escolha": round(st["n"] / total, 3), "winrate": round(st["v"] / st["n"], 3)})
        rows.sort(key=lambda r: -r["partidas"])
        return rows[:top]

    def runes_for(self, role: str, champ: str | None, dd) -> dict | None:
        """Página de runas e feitiços mais usados pelo campeão na rota em elo alto (com vitória)."""
        if not self.available or not champ:
            return None
        key = f"{role}|{_k(champ)}"
        pages = (self.data.get("runas") or {}).get(key) or {}
        if not pages:
            return None
        games = sum(n for n, _ in pages.values())
        page, (n, v) = max(pages.items(), key=lambda x: x[1][0])
        prim, sub, sel, frag = json.loads(page)
        # runa principal (keystone) mais usada, mesmo que a página exata varie
        keystones = defaultdict(lambda: [0, 0])
        for pg, (nn, vv) in pages.items():
            ks = json.loads(pg)[2][0]
            keystones[ks][0] += nn
            keystones[ks][1] += vv
        ks_rows = sorted(keystones.items(), key=lambda x: -x[1][0])[:3]
        out = {"partidas": games,
               "pagina_mais_usada": {"arvore": dd.rune_name(prim), "secundaria": dd.rune_name(sub),
                                     "runas": [dd.rune_name(x) for x in sel], "fragmentos_ids": frag,
                                     "uso": round(n / games, 2), "partidas": n, "winrate": round(v / n, 3) if n else None},
               "runa_principal": [{"runa": dd.rune_name(k), "uso": round(nn / games, 2), "winrate": round(vv / nn, 3)}
                                  for k, (nn, vv) in ks_rows]}
        sp = (self.data.get("feiticos") or {}).get(key) or {}
        if sp:
            pair, (nn, vv) = max(sp.items(), key=lambda x: x[1][0])
            out["feiticos"] = {"par": [dd.spell_name(x) for x in json.loads(pair)], "uso": round(nn / sum(a for a, _ in sp.values()), 2)}
        return out

    def likely_role(self, champ: str | None) -> str | None:
        """Rota em que o campeão mais aparece em elo alto (o cliente não mostra a rota dos inimigos)."""
        if not self.available or not champ:
            return None
        best, n = None, 0
        for role in ROLES:
            st = self.data.get("lane", {}).get(f"{role}|{_k(champ)}")
            if st and st["n"] > n:
                best, n = role, st["n"]
        return best
