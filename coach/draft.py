"""Ajuda no draft (seleção de campeões), lendo a API local do cliente do LoL (LCU).

SÓ LEITURA, com UMA exceção: este módulo só faz GET, exceto aceitar a partida encontrada
(POST /lol-matchmaking/v1/ready-check/accept), liberado pelo usuário em 2026-11-09. Ele nunca entra
na fila, escolhe, bane nem muda mais nada no cliente; e não aceita se você recusou.
(A leitura do cliente é exceção à regra "só Live Client Data API" liberada em 2026-11-05.)

Formato conferido no schema gerado do cliente (mingweisamuel.com/lcu-schema) e num lockfile real:
- lockfile na pasta do LoL: "LeagueClient:<pid>:<porta>:<senha>:https"; autenticação básica riot:<senha>
- GET /lol-gameflow/v1/gameflow-phase  -> "None" | "Lobby" | "ChampSelect" | "InProgress" | ...
- GET /lol-champ-select/v1/session     -> localPlayerCellId, myTeam[] e theirTeam[] (cellId, championId,
  championPickIntent, assignedPosition, spell1Id, spell2Id), actions[][] (actorCellId, championId,
  completed, type "ban"/"pick"), bans {myTeamBans, theirTeamBans}, timer {phase}
O cliente não mostra a rota dos inimigos: ela é deduzida pela rota em que o campeão mais aparece em elo alto."""
from __future__ import annotations

import itertools
import json
import time
from pathlib import Path

import httpx

from .collector import _ssl_context
from .config import ROOT

POS = {"top": "TOP", "jungle": "JUNGLE", "middle": "MIDDLE", "bottom": "BOTTOM", "utility": "UTILITY"}
ROLE_PT = {"TOP": "topo", "JUNGLE": "selva", "MIDDLE": "meio", "BOTTOM": "atirador", "UTILITY": "suporte"}


class LcuClient:
    """Cliente do LoL, só GET. Relê o lockfile se a conexão cair (a porta muda quando o cliente reinicia)."""

    def __init__(self, cfg: dict):
        cc = cfg["cliente"]
        self.folders = [Path(cc.get("pasta", r"C:\Riot Games\League of Legends")),
                        Path(r"C:\Riot Games\League of Legends"), Path(r"D:\Riot Games\League of Legends")]
        self.pem = ROOT / cfg["api"]["certificado"]
        self.client: httpx.Client | None = None

    def _connect(self) -> bool:
        for folder in self.folders:
            lock = folder / "lockfile"
            if lock.exists():
                try:
                    _, _, port, password, proto = lock.read_text(encoding="utf-8").strip().split(":")
                except ValueError:
                    continue
                try:
                    verify = _ssl_context(self.pem)
                except Exception:
                    verify = False          # só 127.0.0.1
                self.client = httpx.Client(base_url=f"{proto}://127.0.0.1:{port}", auth=("riot", password),
                                           verify=verify, timeout=3, trust_env=False)
                return True
        return False

    def get(self, path: str):
        if self.client is None and not self._connect():
            return None
        try:
            r = self.client.get(path)
        except httpx.HTTPError:
            self.client = None              # cliente fechou ou reiniciou: relê o lockfile na próxima
            return None
        return r.json() if r.status_code == 200 else None

    def phase(self) -> str | None:
        return self.get("/lol-gameflow/v1/gameflow-phase")

    def ready_check(self) -> dict | None:
        return self.get("/lol-matchmaking/v1/ready-check")

    def accept_ready_check(self) -> bool:
        """A ÚNICA ação no cliente: aceitar a partida encontrada (liberada pelo usuário)."""
        if self.client is None and not self._connect():
            return False
        try:
            r = self.client.post("/lol-matchmaking/v1/ready-check/accept")
        except httpx.HTTPError:
            self.client = None
            return False
        return r.status_code in (200, 204)

    def session(self) -> dict | None:
        return self.get("/lol-champ-select/v1/session")


BAN_SCHEMA = {
    "type": "object",
    "properties": {
        "ban": {"type": "string", "description": "nome do campeão a banir, como no Data Dragon em português"},
        "motivo": {"type": "string", "description": "por quê, até 14 palavras, falado"},
        "alternativas": {"type": "array", "items": {"type": "string"}, "description": "até 2 outros bans"},
    },
    "required": ["ban", "motivo", "alternativas"],
    "additionalProperties": False,
}


def pick_schema(names: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "pick": {"type": "string", "enum": names},
            "motivo": {"type": "string", "description": "por quê, até 14 palavras, falado"},
            "cuidado": {"type": "string", "description": "a maior ameaça do time inimigo pra esse pick, até 14 palavras"},
        },
        "required": ["pick", "motivo", "cuidado"],
        "additionalProperties": False,
    }


class DraftAdvisor:
    def __init__(self, cfg: dict, dd, champs: dict, elo, sonnet_client, voice, write_window, lcu=None):
        self.cfg, self.dd, self.champs, self.elo = cfg, dd, champs, elo
        self.sonnet, self.voice, self.write_window = sonnet_client, voice, write_window
        self.lcu = lcu or LcuClient(cfg)
        self.interval = float(cfg["cliente"].get("intervalo_s", 2))
        self.last_poll = 0.0
        self.log_path = ROOT / "logs" / "draft.jsonl"
        self.accept = bool(cfg["cliente"].get("aceitar_partida", False))
        self.accept_delay = float(cfg["cliente"].get("aceitar_atraso_s", 1))
        self.ready_seen: float | None = None    # quando a tela de partida encontrada apareceu
        self.ready_done = False
        self.reset()

    def reset(self) -> None:
        self.game_id = None
        self.said_ban = False
        self.pick_key = None            # (inimigos, bans) da última sugestão de pick
        self.spoken_pick = None         # (pick sugerido, oponente provável) da última fala
        self.locked_said = None
        self.lines: dict[str, str] = {}

    # ---------------- ciclo ----------------
    def tick(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if now - self.last_poll < self.interval:
            return
        self.last_poll = now
        phase = self.lcu.phase()
        if phase == "ReadyCheck":
            self._ready_check(now)
            return
        self.ready_seen, self.ready_done = None, False
        if phase != "ChampSelect":
            if self.game_id is not None:
                self.reset()
            return
        sess = self.lcu.session()
        if not sess:
            return
        st = self.parse(sess)
        if st["game_id"] != self.game_id:
            self.reset()
            self.game_id = st["game_id"]
        if not self.said_ban and not st["my_locked"]:
            self.said_ban = True
            self._ban(st)
        key = (tuple(st["enemies"]), tuple(sorted(st["bans"])))
        if not st["my_locked"] and key != self.pick_key and (st["enemies"] or self._main_banned(st)):
            self.pick_key = key
            self._pick(st)
        if st["my_locked"] and self.locked_said != st["my_champ"]:
            self.locked_said = st["my_champ"]
            self._locked(st)

    # ---------------- leitura da sessão ----------------
    def _ready_check(self, now: float) -> None:
        """Partida encontrada: aceita depois de um instante, só se você ainda não respondeu."""
        if not self.accept or self.ready_done:
            return
        rc = self.lcu.ready_check() or {}
        if rc.get("state") != "InProgress" or rc.get("playerResponse") != "None":
            if rc.get("playerResponse") in ("Accepted", "Declined"):
                self.ready_done = True          # você já respondeu (inclusive recusou): não mexe
            return
        if self.ready_seen is None:
            self.ready_seen = now
        if now - self.ready_seen < self.accept_delay:
            return
        self.ready_done = True
        if self.lcu.accept_ready_check():
            print("[draft] partida aceita")
            self._say("Partida aceita")

    def parse(self, sess: dict) -> dict:
        cell = sess.get("localPlayerCellId")
        name = lambda k: self.dd.champ_by_key(k) if k else None
        me = next((p for p in sess.get("myTeam") or [] if p.get("cellId") == cell), {})
        actions = [a for group in sess.get("actions") or [] for a in group]
        bans = {name(a.get("championId")) for a in actions if a.get("type") == "ban" and a.get("completed")}
        b = sess.get("bans") or {}
        bans |= {name(x) for x in (b.get("myTeamBans") or []) + (b.get("theirTeamBans") or [])}
        bans.discard(None)
        my_locked = any(a.get("type") == "pick" and a.get("actorCellId") == cell and a.get("completed") for a in actions)
        role = POS.get(str(me.get("assignedPosition") or "").lower())
        if role is None:
            fixed = str(self.cfg["jogador"].get("rota", "auto")).upper()
            role = fixed if fixed in POS.values() else None
        allies = [name(p.get("championId") or p.get("championPickIntent")) for p in sess.get("myTeam") or []
                  if p.get("cellId") != cell]
        enemies = [name(p.get("championId")) for p in sess.get("theirTeam") or [] if p.get("championId")]
        return {"game_id": sess.get("gameId") or id(sess), "role": role, "phase": (sess.get("timer") or {}).get("phase"),
                "my_champ": name(me.get("championId") or me.get("championPickIntent")), "my_locked": my_locked,
                "allies": [a for a in allies if a], "enemies": [e for e in enemies if e], "bans": bans,
                "spells": [me.get("spell1Id"), me.get("spell2Id")]}

    def pool(self, role: str | None) -> list[str]:
        """Campeões da pool pra rota, na ordem do champions.yaml (o primeiro é o principal).
        'draft: false' no champions.yaml tira o campeão das sugestões (o perfil continua pro jogo)."""
        ok = [cid for cid, p in self.champs.items() if cid != "_generico" and p.get("draft", True) is not False]
        out = [cid for cid in ok if role is None or str(self.champs[cid].get("rota", "")).upper() == role]
        return out or ok

    def personal(self) -> dict[str, dict]:
        """Seu histórico por campeão, das partidas gravadas em logs/ (analise.json + resultado.txt)."""
        out: dict[str, dict] = {}
        for d in (ROOT / "logs").iterdir() if (ROOT / "logs").exists() else []:
            f = d / "analise.json"
            if d.name.endswith("_replay") or not f.exists():
                continue
            try:
                n = json.loads(f.read_text(encoding="utf-8"))["fatos"]["numeros"]
            except (ValueError, KeyError):
                continue
            res = n.get("resultado", "desconhecido")
            over = d / "resultado.txt"
            if over.exists():
                raw = over.read_text(encoding="utf-8").strip().lower()
                if "troll" in raw or "excluir" in raw:
                    continue
                res = raw.split("(")[0].strip() or res
            if n.get("duracao_s", 0) < 300 or not n.get("campeao_id"):
                continue
            row = out.setdefault(n["campeao_id"], {"partidas": 0, "vitorias": 0, "derrotas": 0})
            row["partidas"] += 1
            row["vitorias"] += res == "vitória"
            row["derrotas"] += res == "derrota"
        return out

    def _available(self, st: dict) -> list[str]:
        taken = st["bans"] | set(st["allies"]) | set(st["enemies"])
        return [c for c in self.pool(st["role"]) if c not in taken]

    def _main_banned(self, st: dict) -> bool:
        pool = self.pool(st["role"])
        return bool(pool) and pool[0] in st["bans"] | set(st["enemies"])

    # ---------------- sugestões ----------------
    def _ban(self, st: dict) -> None:
        main = (self._available(st) or [None])[0]
        role = st["role"]
        ctx = {"pedido": ("Sugira um ban pra esse jogador de solo queue. Ele joga a rota e vai pegar o campeão principal "
                          "da pool. Ban bom tira um campeão que ganha do principal dele na rota (counter, que anula ou "
                          "pune o kit dele) ou um campeão muito forte no elo. Use os dados de elo alto quando houver "
                          "amostra; se não, use conhecimento do jogo. Responda o nome em português como no Data Dragon."),
               "rota": role, "campeao_principal": self._cname(main), "pool": [self._cname(c) for c in self.pool(role)],
               "ja_banidos": [self._cname(c) for c in st["bans"]]}
        if self.elo and self.elo.available and role:
            ctx["mais_jogados_na_rota_elo_alto"] = self.elo.meta_role(role, self.dd, 10)
            if main:
                ctx["seu_principal_elo_alto"] = self.elo.lane(role, main, None)
        ans = self._ask(BAN_SCHEMA, ctx)
        if ans:
            ans = {k: v.strip().rstrip(".") if isinstance(v, str) else v for k, v in ans.items()}
        ban = ans and self.dd.champ_id(ans.get("ban"))
        if ban:
            alt = [a for a in ans.get("alternativas", []) if self.dd.champ_id(a)][:2]
            self.lines["ban"] = (f"BAN: {self._cname(ban)} — {ans.get('motivo', '')}"
                                 + (f" (ou {', '.join(alt)})" if alt else ""))
            self._say(f"Sugestão de ban: {self._cname(ban)}. {ans.get('motivo', '')}")
        else:
            top = [r["campeao"] for r in (self.elo.meta_role(role, self.dd, 3) if self.elo and role else [])]
            self.lines["ban"] = "BAN: " + (f"campeões mais fortes da rota no elo alto: {', '.join(top)}" if top else
                                           "sem sugestão (sem Sonnet nem dados de elo alto)")
        self._show(st)

    def role_probs(self, champ: str) -> dict[str, float]:
        """Chance de cada rota pro campeão: partidas dele em cada rota no elo alto (+1 de suavização).
        Sem dados, usa a classe do Data Dragon."""
        counts = {}
        if self.elo and self.elo.available:
            for r in POS.values():
                counts[r] = ((self.elo.data.get("lane") or {}).get(f"{r}|{champ.lower()}") or {}).get("n", 0)
        if not sum(counts.values()):
            tags = self.dd.champions.get(champ, {}).get("tags", [])
            guess = {"Marksman": "BOTTOM", "Support": "UTILITY", "Tank": "TOP", "Fighter": "TOP",
                     "Mage": "MIDDLE", "Assassin": "MIDDLE"}
            counts = {r: 0 for r in POS.values()}
            for t in tags[:1]:
                counts[guess.get(t, "MIDDLE")] += 5
        tot = sum(counts.values()) + len(counts)
        return {r: (n + 1) / tot for r, n in counts.items()}

    def lane_opponents(self, enemies: list[str], role: str | None) -> list[tuple[str, float]]:
        """Quem do time inimigo está na sua rota, olhando o time todo: cada campeão numa rota diferente,
        e a combinação mais provável pesa mais (Diana mid/selva + Malzahar só mid -> Diana na selva)."""
        if not role or not enemies:
            return []
        probs = {e: self.role_probs(e) for e in enemies}
        roles = list(POS.values())
        weight = {e: 0.0 for e in enemies}
        total = 0.0
        for combo in itertools.permutations(roles, len(enemies)):
            w = 1.0
            for e, r in zip(enemies, combo):
                w *= probs[e][r]
            total += w
            for e, r in zip(enemies, combo):
                if r == role:
                    weight[e] += w
        out = [(e, weight[e] / total) for e in enemies if total and weight[e] / total >= 0.2]
        return sorted(out, key=lambda x: -x[1])

    def _pick(self, st: dict) -> None:
        cands = self._available(st)
        role = st["role"]
        if not cands:
            self.lines["pick"] = "PICK: os campeões da sua pool pra essa rota foram banidos ou escolhidos."
            self._say("Sua pool foi banida. Pegue um campeão que você conhece bem")
            self._show(st)
            return
        opps = self.lane_opponents(st["enemies"], role)
        sure = opps and opps[0][1] >= 0.6
        lane_opp = opps[0][0] if sure else None
        if opps:
            self.lines["oponente"] = "OPONENTE PROVÁVEL: " + ", ".join(f"{self._cname(e)} ({p:.0%})" for e, p in opps[:3])
        else:
            self.lines["oponente"] = "OPONENTE PROVÁVEL: ainda ninguém do time inimigo parece ser da sua rota" 
        mine = self.personal()
        ctx = {"pedido": ("Escolha o campeão da pool dele pra essa partida. O primeiro da lista é o PRINCIPAL: é o que ele "
                          "mais joga e mais ganha. Mantenha o principal, a não ser que ele tenha sido banido ou escolhido, "
                          "ou que o provável oponente de rota seja um counter claro dele (kit que anula o principal). "
                          "A vitória geral do campeão em Mestre+ NÃO é motivo pra trocar: o histórico do próprio jogador "
                          "vale mais. Nunca sugira campeão fora da pool. Considere o provável oponente de rota."),
               "principal": self._cname(self.pool(role)[0]) if self.pool(role) else None,
               "seu_historico": {self._cname(c): mine.get(c, {"partidas": 0}) for c in cands},
               "rota": role, "candidatos_da_pool": [self._cname(c) for c in cands],
               "principal_banido": self._main_banned(st),
               "inimigos": [{"campeao": self._cname(e), "chance_de_ser_da_sua_rota": round(dict(opps).get(e, 0.0), 2)}
                            for e in st["enemies"]],
               "aliados": [self._cname(a) for a in st["allies"]],
               "provavel_oponente_de_rota": self._cname(lane_opp) if lane_opp else None,
               "aviso": ("oponente de rota ainda incerto: não fale como se fosse certo"
                         if not lane_opp else "oponente de rota provável, calculado com o time inimigo inteiro")}
        if self.elo and self.elo.available and role:
            ctx["elo_alto"] = {self._cname(c): self.elo.lane(role, c, lane_opp) for c in cands}
        ans = self._ask(pick_schema([self._cname(c) for c in cands]), ctx)
        if ans:
            ans = {k: v.strip().rstrip(".") if isinstance(v, str) else v for k, v in ans.items()}
        if ans and ans.get("pick"):
            self.lines["pick"] = f"PICK: {ans['pick']} — {ans.get('motivo', '')}. Cuidado: {ans.get('cuidado', '')}"
            if lane_opp:
                vs = f"{self._cname(lane_opp)} no {ROLE_PT.get(role, 'sua rota')}"
            elif len(opps) >= 2:
                vs = f"{self._cname(opps[0][0])} ou {self._cname(opps[1][0])} no {ROLE_PT.get(role, 'sua rota')}"
            else:
                vs = "Principal banido" if self._main_banned(st) else "Oponente ainda indefinido"
            # só fala de novo se mudou o pick sugerido ou quem deve ser o oponente
            spoken = (ans["pick"], lane_opp, tuple(e for e, _ in opps[:2]))
            if spoken != self.spoken_pick:
                self.spoken_pick = spoken
                self._say(f"{vs}: pegue {ans['pick']}. {ans.get('motivo', '')}")
        else:
            self.lines["pick"] = f"PICK: {self._cname(cands[0])} (primeiro da sua pool disponível)"
            if self._main_banned(st):
                self._say(f"Principal banido: pegue {self._cname(cands[0])}")
        self._show(st)

    def _locked(self, st: dict) -> None:
        champ, role = st["my_champ"], st["role"]
        r = self.elo.runes_for(role, champ, self.dd) if self.elo and role else None
        if r:
            pg = r["pagina_mais_usada"]
            self.lines["runas"] = (f"RUNAS ({r['partidas']} partidas de {self._cname(champ)} em Mestre+): "
                                   f"{pg['arvore']}: {', '.join(pg['runas'][:4])} | {pg['secundaria']}: "
                                   f"{', '.join(pg['runas'][4:])} — {pg['uso']:.0%} usam, {pg['winrate']:.0%} de vitória")
            if r.get("feiticos"):
                self.lines["runas"] += f" | Feitiços: {' e '.join(r['feiticos']['par'])}"
            self._say(f"Runas: {pg['runas'][0]}, com {pg['secundaria']} de secundária")
        else:
            self.lines["runas"] = ("RUNAS: ainda sem dados de runas do elo alto. Gere uma chave nova da Riot e rode "
                                   "baixar_estatisticas.bat (ele completa as partidas com as runas).")
        self._show(st)

    # ---------------- saída ----------------
    def _cname(self, cid: str | None) -> str:
        return self.dd.champ_name(cid) if cid else "?"

    def _show(self, st: dict) -> None:
        L = [f"DRAFT — você: {ROLE_PT.get(st['role'], 'rota ?')}"
             + (f" | escolhendo {self._cname(st['my_champ'])}" if st["my_champ"] else ""), ""]
        L += [self.lines[k] for k in ("ban", "oponente", "pick", "runas") if k in self.lines]
        L += ["", f"Banidos: {', '.join(self._cname(c) for c in sorted(st['bans'])) or '-'}",
              f"Seu time: {', '.join(self._cname(c) for c in st['allies']) or '-'}",
              f"Inimigos: {', '.join(self._cname(c) for c in st['enemies']) or '-'}",
              "", "(só leitura: o coach não escolhe nem bane nada por você)"]
        text = "\n".join(L)
        self.write_window(text)
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"hora": time.strftime("%Y-%m-%d %H:%M:%S"), "rota": st["role"],
                                    "banidos": sorted(st["bans"]), "inimigos": st["enemies"], "linhas": self.lines},
                                   ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _say(self, text: str) -> None:
        if self.voice is not None:
            self.voice.say(text)

    def _ask(self, schema: dict, ctx: dict) -> dict | None:
        if self.sonnet is None:
            return None
        from .sonnet import Job
        try:
            a = self.sonnet.ask(Job("draft", 0, ctx, schema))
        except Exception as e:
            print(f"[draft] Sonnet falhou: {e}")
            return None
        return a.data if a.data and not a.error else None
