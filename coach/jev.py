"""Jev (TypeSafe AI): decisão rápida entre opções fixas a partir do estado resumido.

API (docs.typesafe.ai/api): POST {base}/v1/systemone, Authorization: Bearer <chave>,
corpo {"model", "state", "questions": {nome: {"type": "choice", "instructions", "criteria"}}};
resposta {"model", "answers": {nome: {"choice", "confidence", "probabilities"}}, "usage"}.
Dois provedores com o mesmo formato:
- TypeSafe direto: chave em TYPESAFE_API_KEY, base https://api.typesafe.ai (ou TYPESAFE_API_BASE).
- OpenRouter (openrouter.ai/docs/guides/community/typesafe-sdk): chave em OPENROUTER_API_KEY,
  base https://openrouter.ai/api, modelo em OPENROUTER_JEV_MODEL (ex.: typesafe/jev-1.13);
  a resposta traz ainda usage.cost em dólares."""
from __future__ import annotations

import os
import queue
import threading
import time
from dataclasses import dataclass

import httpx

from .state import GameState, lane_advantage

PROVIDERS = {
    "typesafe": {"key_env": "TYPESAFE_API_KEY", "base": "https://api.typesafe.ai", "base_env": "TYPESAFE_API_BASE",
                 "model_env": None, "model": "jev-latest"},
    "openrouter": {"key_env": "OPENROUTER_API_KEY", "base": "https://openrouter.ai/api", "base_env": None,
                   "model_env": "OPENROUTER_JEV_MODEL", "model": "typesafe/jev-1.13"},
}
QUESTION = "decisao"

OPTIONS = {
    "pressionar": "Trocar dano ou dar all-in no oponente de lane, ou forçar luta: o jogador está mais forte agora.",
    "jogar_seguro": "Recuar, evitar trocas e lutas: o jogador ou o time está em desvantagem ou em perigo.",
    "farmar": "Situação neutra: focar em matar tropas/monstros e ganhar ouro sem arriscar.",
    "voltar_base": "Voltar à base agora: vida baixa e/ou ouro guardado suficiente pra comprar itens.",
    "agrupar_objetivo": "Ir com o time pro objetivo (dragão, barão, arauto, vastilarvas) que nasce logo ou está vivo.",
    "ajudar_vizinho": "Ajudar o caçador ou uma lane vizinha (gank, rotação, cobrir invasão).",
}

INSTRUCTIONS = (
    "Você é um coach de League of Legends. Com base no estado da partida, escolha a melhor decisão "
    "para o jogador nos próximos 15 a 30 segundos. A posição dos inimigos, a vida atual deles e os "
    "tempos de recarga deles são desconhecidos: considere os inimigos com vida cheia. "
    "Vantagem de lane positiva favorece pressionar; vida baixa ou ouro alto favorecem voltar à base; "
    "objetivo nascendo em menos de 60 segundos favorece agrupar."
)


@dataclass
class JevAnswer:
    game_time: float          # tempo de jogo do estado enviado
    choice: str
    confidence: float
    probabilities: dict[str, float]
    model: str
    latency: float
    input_tokens: int
    cost: float
    state: dict


def summarize(s: GameState) -> dict:
    """Estado resumido: só campeões e números, sem nomes de jogador."""
    o = s.objectives
    t = s.game_time
    adv, reasons = lane_advantage(s)

    def obj_timer(spawn):
        if spawn is None:
            return "não nasce mais"
        return "vivo" if t >= spawn else f"nasce em {int(spawn - t)}s"

    opp = s.opponent
    ally_gold = sum(p.item_value for p in s.allies)
    enemy_gold = sum(p.item_value for p in s.enemies)
    return {
        "tempo_de_jogo": f"{int(t) // 60}:{int(t) % 60:02d}",
        "fase": "lane" if t < 14 * 60 else "meio/fim de jogo",
        "jogador": {
            "campeao": s.me.champ_name, "rota": s.role, "nivel": s.me.level,
            "vida_pct": round(s.hp_pct * 100), "ouro_guardado": int(s.gold),
            "itens_completos": len(s.me.completed), "abates_mortes_assist": f"{s.me.kills}/{s.me.deaths}/{s.me.assists}",
            "farm": s.me.cs, "morto": s.me.is_dead, "na_base": s.at_base(),
        },
        "oponente_de_lane": None if opp is None else {
            "campeao": opp.champ_name, "nivel": opp.level, "itens_completos": len(opp.completed),
            "abates_mortes_assist": f"{opp.kills}/{opp.deaths}/{opp.assists}", "farm": opp.cs, "morto": opp.is_dead,
        },
        "vantagem_de_lane": {"pontuacao": adv, "motivos": reasons},
        "diferenca_ouro_em_itens_do_time": ally_gold - enemy_gold,
        "inimigos_mortos": s.enemies_dead,
        "aliados_mortos": s.allies_dead,
        "objetivos": {
            ("dragao_anciao" if o.elder_next else "dragao"): obj_timer(o.next_dragon),
            "barao": obj_timer(o.next_baron),
            "arauto": obj_timer(o.next_herald),
            "vastilarvas": obj_timer(o.next_grubs),
            "dragoes_do_time": o.dragons.get(s.me.team, 0),
            "dragoes_inimigos": sum(v for k, v in o.dragons.items() if k != s.me.team),
        },
    }


class JevClient:
    def __init__(self, cfg: dict):
        jc = cfg["jev"]
        wanted = str(jc.get("provedor", "auto")).lower()
        order = [wanted] if wanted in PROVIDERS else ["typesafe", "openrouter"]
        # auto: usa o primeiro provedor que tiver chave no ambiente
        self.provider = next((p for p in order if os.environ.get(PROVIDERS[p]["key_env"])), order[0])
        pv = PROVIDERS[self.provider]
        self.key_env = pv["key_env"]
        self.key = os.environ.get(pv["key_env"], "")
        self.base = ((pv["base_env"] and os.environ.get(pv["base_env"])) or pv["base"]).rstrip("/")
        cfg_model = jc.get("modelo", "auto")
        self.model = (cfg_model if cfg_model != "auto" else None) or \
            (pv["model_env"] and os.environ.get(pv["model_env"])) or pv["model"]
        self.timeout = float(jc.get("timeout_s", 4.0))
        self.client = httpx.Client(timeout=self.timeout)

    @property
    def configured(self) -> bool:
        return bool(self.key)

    def decide(self, state: dict) -> dict:
        body = {
            "model": self.model,
            "state": state,
            "questions": {QUESTION: {"type": "choice", "instructions": INSTRUCTIONS, "criteria": OPTIONS}},
        }
        headers = {"Authorization": f"Bearer {self.key}"}
        if self.provider == "openrouter":
            headers["X-Title"] = "Coach LoL"   # identificação opcional do app no OpenRouter
        r = self.client.post(f"{self.base}/v1/systemone", json=body, headers=headers)
        if r.status_code != 200:
            try:
                detail = r.json().get("detail")
            except ValueError:
                detail = r.text[:200]
            raise JevError(r.status_code, detail)
        return r.json()


class JevAdvisor:
    """Decide quando perguntar ao Jev e se a resposta vira fala."""

    # Regras determinísticas que já cobrem a mesma ideia (prefixos de chave)
    RELATED = {
        "voltar_base": ("hp_", "gold"),
        "jogar_seguro": ("hp_", "lane_seguro", "opp_lvl", "num_disadv", "respawn_safe", "died_opp", "baron_theirs"),
        "pressionar": ("lane_pressionar", "lvl2_first", "lvl3_first", "lvl6_first", "my_spike", "opp_dead"),
        "agrupar_objetivo": ("obj_", "num_adv", "ace", "baron_ours", "team_ahead"),
        "farmar": ("lane_seguro",),
        "ajudar_vizinho": (),
    }
    COOLDOWN = {"farmar": 150}  # repetir a mesma escolha: padrão 90s

    def __init__(self, cfg: dict, worker: JevWorker, speech, log):
        jc = cfg["jev"]
        self.threshold = float(jc.get("limiar_confianca", 0.7))
        self.interval = float(jc.get("intervalo_s", 20))
        self.max_delay = float(jc.get("atraso_max_s", 6))
        self.worker, self.speech, self.log = worker, speech, log
        self.rules_cfg = cfg["regras"]
        self.last_req = -1e9
        self.last_choice: str | None = None
        self.stats = {"pedidos": 0, "respostas": 0, "faladas": 0, "tokens": 0, "custo": 0.0}

    def tick(self, s: GameState, changes) -> None:
        while not self.worker.results.empty():
            self._handle(self.worker.results.get(), s)
        if s.me.is_dead or s.game_time < 90 or s.game_mode not in ("CLASSIC", "SWIFTPLAY", "PRACTICETOOL", ""):
            return
        important = any(
            c.kind in ("item_completed", "my_respawn")
            or (c.kind == "level_up" and c.data["level"] in (2, 3, 6))
            or (c.kind == "event" and c.data.get("EventName") in ("ChampionKill", "DragonKill", "BaronKill", "TurretKilled"))
            for c in changes)
        gap = s.game_time - self.last_req
        if gap >= self.interval or (important and gap >= 8):
            if self.worker.request(s.game_time, summarize(s)):
                self.last_req = s.game_time
                self.stats["pedidos"] += 1

    def _handle(self, a: JevAnswer, s: GameState) -> None:
        from .rules import NORMAL, Advice
        self.stats["respostas"] += 1
        self.stats["tokens"] += a.input_tokens
        self.stats["custo"] += a.cost
        rec = {"tempo_jogo": round(a.game_time, 1), "escolha": a.choice, "confianca": round(a.confidence, 3),
               "probabilidades": {k: round(v, 3) for k, v in a.probabilities.items()}, "modelo": a.model,
               "latencia_s": round(a.latency, 2), "tokens_entrada": a.input_tokens, "custo_usd": a.cost,
               "estado": a.state}
        status = None
        if s.game_time - a.game_time > self.max_delay:
            status = "atrasada"
        elif a.confidence < self.threshold:
            status = "abaixo_do_limiar"
        elif s.me.is_dead:
            status = "jogador_morto"
        elif a.choice not in OPTIONS:
            status = "opcao_desconhecida"
        else:
            status = self._conflict(a.choice, s)
        if status is None:
            text = phrase(a.choice, s)
            self.speech.push(Advice(text, NORMAL, f"jev_{a.choice}", cooldown=self.COOLDOWN.get(a.choice, 90), ttl=10,
                                    created=s.game_time, reason=f"Jev {a.choice} {a.confidence:.0%}",
                                    check=_still_valid(a.choice)), s.game_time)
            status = "enviada_pra_fala"
            self.stats["faladas"] += 1
            self.last_choice = a.choice
        rec["status"] = status
        self.log(rec)

    def _conflict(self, choice: str, s: GameState) -> str | None:
        """Motivo pra não falar: repete uma regra, contradiz uma regra recente ou o próprio cálculo."""
        t = s.game_time

        def spoken_since(prefixes: tuple, window: float) -> str | None:
            if not prefixes:
                return None
            return next((k for k, kt in self.speech.last_by_key.items()
                         if t - kt < window and k.startswith(prefixes)), None)

        last_same = self.speech.last_by_key.get(f"jev_{choice}")
        if last_same is not None and t - last_same < self.COOLDOWN.get(choice, 90):
            return "repetida"
        k = spoken_since(self.RELATED.get(choice, ()), 25)
        if k:
            return f"redundante ({k})"
        opposite = {"pressionar": "jogar_seguro", "jogar_seguro": "pressionar"}.get(choice)
        if opposite:
            k = spoken_since(self.RELATED[opposite] + (f"jev_{opposite}",), 45)
            if k:
                return f"contradiz ({k})"
        if choice == "pressionar":
            # 11:58 "jungler sumido" e 12:13 "pressione" -> gank às 12:21 (partida de 2026-11-04)
            k = spoken_since(("jg_missing",), 60)
            if k:
                return f"contradiz ({k}: jungler inimigo sumido)"
            adv, _ = lane_advantage(s)
            if s.hp_pct < 0.4 or (s.opponent is not None and adv <= self.rules_cfg["vantagem_seguro"]):
                return f"contradiz o cálculo (vida {s.hp_pct:.0%}, vantagem {adv})"
        return None


def _still_valid(choice: str):
    """Conferido de novo na hora de falar (a fala pode esperar alguns segundos na fila)."""
    if choice == "voltar_base":
        return lambda st: not st.me.is_dead and not st.at_base(30)
    return lambda st: not st.me.is_dead


def phrase(choice: str, s: GameState) -> str:
    """Frase curta (até ~8 palavras) pra cada opção, adaptada à rota e ao objetivo."""
    role = s.role
    if choice == "pressionar":
        return "Pressione: invada ou procure gank" if role == "JUNGLE" else "Hora de pressionar o oponente"
    if choice == "jogar_seguro":
        return "Jogue seguro agora, sem lutas"
    if choice == "farmar":
        return "Foque no farm agora"
    if choice == "voltar_base":
        return "Boa hora pra voltar à base"
    if choice == "agrupar_objetivo":
        o, t = s.objectives, s.game_time
        soon = [(spawn, name) for spawn, name in (
            (o.next_dragon, "Ancião" if o.elder_next else "dragão"), (o.next_baron, "Barão"),
            (o.next_herald, "Arauto"), (o.next_grubs, "vastilarvas")) if spawn is not None and spawn - t < 90]
        return f"Agrupem pro {min(soon)[1]}" if soon else "Agrupe com o time"
    if choice == "ajudar_vizinho":
        return {"JUNGLE": "Procure gank numa lane", "MIDDLE": "Ajude o jungle ou lane vizinha",
                "TOP": "Ajude o jungle no lado de cima"}.get(role, "Ajude seu jungle no lado de baixo")
    return choice


class JevError(Exception):
    def __init__(self, status: int, detail):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status


class JevWorker:
    """Faz as chamadas numa thread pra não travar o polling de 1s. Uma requisição por vez."""

    def __init__(self, client: JevClient, log):
        self.client = client
        self.log = log                     # callback(dict) pra gravar no jev.jsonl
        self.results: queue.Queue[JevAnswer] = queue.Queue()
        self._busy = threading.Event()
        self.failures = 0
        self.disabled_reason: str | None = None
        self.backoff_until = 0.0

    @property
    def busy(self) -> bool:
        return self._busy.is_set()

    def request(self, game_time: float, state: dict) -> bool:
        if self.busy or self.disabled_reason or time.time() < self.backoff_until:
            return False
        self._busy.set()
        threading.Thread(target=self._run, args=(game_time, state), daemon=True).start()
        return True

    def _run(self, game_time: float, state: dict) -> None:
        t0 = time.time()
        try:
            resp = self.client.decide(state)
            ans = resp["answers"][QUESTION]
            usage = resp.get("usage") or {}
            a = JevAnswer(game_time, ans["choice"], float(ans.get("confidence") or 0.0),
                          ans.get("probabilities") or {}, resp.get("model", ""), time.time() - t0,
                          int(usage.get("input_tokens", 0)), float(usage.get("cost") or 0.0), state)
            self.failures = 0
            self.results.put(a)
        except JevError as e:
            self._fail(game_time, str(e), fatal=e.status in (401, 403))
        except (httpx.HTTPError, KeyError, ValueError) as e:
            self._fail(game_time, f"{type(e).__name__}: {e}")
        finally:
            self._busy.clear()

    def _fail(self, game_time: float, msg: str, fatal: bool = False) -> None:
        self.failures += 1
        self.log({"tempo_jogo": round(game_time, 1), "erro": msg})
        if fatal:
            self.disabled_reason = msg
            print(f"[jev] desativado: {msg} (confira a chave {self.client.key_env})")
        elif self.failures >= 3:
            self.backoff_until = time.time() + 60
            print(f"[jev] {self.failures} falhas seguidas ({msg}); pausa de 60s")
