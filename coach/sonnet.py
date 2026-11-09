"""Fase 3: Sonnet (Anthropic) nos momentos sem pressa.

- Começo da partida: plano do matchup (até 3 frases curtas).
- Morte/base: escolhe o próximo item ENTRE os candidatos que o cálculo ranqueou (os números vêm do
  cálculo, o modelo só pesa o que o cálculo não modela e explica).
- A cada ~3-4 min: reavaliação curta do macro.

Provedores (SDK oficial da Anthropic):
- Anthropic direto: ANTHROPIC_API_KEY, modelo claude-sonnet-5-5, com fallback de recusa no servidor.
- OpenRouter: OPENROUTER_API_KEY, base https://openrouter.ai/api (aceita o formato da API da Anthropic),
  modelo anthropic/claude-sonnet-5.5 (ou OPENROUTER_SONNET_MODEL). A resposta traz usage.cost.
As chamadas rodam numa thread; nunca travam o polling de 1 s."""
from __future__ import annotations

import json
import os
import queue
import threading
import time
from dataclasses import dataclass, field

import anthropic

from .jev import summarize as jev_summary
from .state import GameState

PROVIDERS = {
    "anthropic": {"key_env": "ANTHROPIC_API_KEY", "base": None, "model_env": "ANTHROPIC_SONNET_MODEL",
                  "model": "claude-sonnet-5-5"},
    "openrouter": {"key_env": "OPENROUTER_API_KEY", "base": "https://openrouter.ai/api",
                   "model_env": "OPENROUTER_SONNET_MODEL", "model": "anthropic/claude-sonnet-5.5"},
}

SYSTEM = """Você é o coach de League of Legends do jogador e fala com ele por voz (texto vira fala) durante a partida, em português do Brasil.

Como escrever as falas:
- Frases curtas e faladas: no máximo 8 palavras por frase (10 na reavaliação de macro).
- Nada de listas, emojis, parênteses ou siglas difíceis de pronunciar; nomes de campeões e itens como aparecem nos dados.
- Concreto e acionável: o que fazer agora ou no próximo minuto.
- Habilidades: o jogador não decora nomes de habilidade. Sempre diga a tecla e o campeão, mais 2 a 4 palavras
  do que ela faz: "o E da Ahri, o beijo que encanta", "a passiva do Zed, dano extra em vida baixa",
  "o R do Malphite, o avanço que derruba". Nunca use só o nome da habilidade. Pras do jogador: "seu Q", "sua W".

O que você sabe e o que não sabe:
- Os dados vêm da Live Client Data API da Riot: campeões, níveis, itens, placar, feitiços, eventos e o estado do jogador.
- Não há posição de ninguém, nem vida atual ou recargas dos inimigos.
- Cite só números que estejam nos dados recebidos; nunca invente números.
- Não assuma feitiços, runas ou itens que não estejam nos dados (por exemplo, só fale de Incendiar se o jogador tiver Incendiar).
- Estatísticas de elo alto, quando vierem, são de partidas ranqueadas Mestre+ baixadas da API da Riot; use como contexto e respeite o tamanho da amostra."""

MATCHUP_SCHEMA = {
    "type": "object",
    "properties": {
        "dica_matchup": {"type": "string", "description": (
            "frase falada de até 18 palavras: a habilidade ou passiva do oponente que mais decide a rota, "
            "pela tecla e campeão com uma descrição curta do que faz, e o que fazer contra ela")},
        "frases": {"type": "array", "items": {"type": "string"},
                   "description": "0 a 2 frases faladas extras (níveis fortes, all-in, primeiro objetivo), até 10 palavras cada"},
        "explicacao": {"type": "string", "description": (
            "texto pra janela, até 8 linhas curtas: habilidades-chave do oponente com recarga, janela de troca "
            "(quando ele gastar o quê), o que respeitar, níveis e itens que mudam a rota")},
    },
    "required": ["dica_matchup", "frases", "explicacao"],
    "additionalProperties": False,
}

MACRO_SCHEMA = {
    "type": "object",
    "properties": {
        "frase": {"type": "string", "description": "uma frase falada, no máximo 10 palavras"},
        "prioridade": {"type": "string", "enum": ["alta", "normal"]},
        "motivo": {"type": "string", "description": "por quê, em uma ou duas frases, pro log"},
    },
    "required": ["frase", "prioridade", "motivo"],
    "additionalProperties": False,
}


def item_schema(names: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "item": {"type": "string", "enum": names},
            "frase": {"type": "string", "description": "por que esse item, no máximo 8 palavras, sem números"},
            "motivo": {"type": "string", "description": "explicação pra janela, até 3 frases"},
        },
        "required": ["item", "frase", "motivo"],
        "additionalProperties": False,
    }


@dataclass
class Job:
    kind: str                 # matchup | item | macro
    game_time: float
    user: dict
    schema: dict
    meta: dict = field(default_factory=dict)


@dataclass
class Answer:
    job: Job
    data: dict | None
    latency: float
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    error: str | None = None


class SonnetClient:
    def __init__(self, cfg: dict):
        sc = cfg["sonnet"]
        wanted = str(sc.get("provedor", "auto")).lower()
        order = [wanted] if wanted in PROVIDERS else ["anthropic", "openrouter"]
        self.provider = next((p for p in order if os.environ.get(PROVIDERS[p]["key_env"])), order[0])
        pv = PROVIDERS[self.provider]
        self.key_env = pv["key_env"]
        key = os.environ.get(pv["key_env"], "")
        cfg_model = sc.get("modelo", "auto")
        self.model = (cfg_model if cfg_model != "auto" else None) or os.environ.get(pv["model_env"] or "") or pv["model"]
        self.effort = str(sc.get("esforco", "low"))
        self.price_in, self.price_out = (float(x) for x in sc.get("preco_por_milhao", [2.0, 10.0]))
        self.client = None
        if key:
            timeout = float(sc.get("timeout_s", 30))
            if self.provider == "openrouter":
                self.client = anthropic.Anthropic(base_url=pv["base"], auth_token=key, api_key=None, timeout=timeout,
                                                  max_retries=1, default_headers={"X-Title": "Coach LoL"})
            else:
                self.client = anthropic.Anthropic(api_key=key, timeout=timeout, max_retries=1)

    @property
    def configured(self) -> bool:
        return self.client is not None

    def ask(self, job: Job) -> Answer:
        t0 = time.time()
        kwargs = dict(
            model=self.model, max_tokens=2000, system=SYSTEM,
            output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": job.schema}},
            messages=[{"role": "user", "content": json.dumps(job.user, ensure_ascii=False)}],
        )
        if self.provider == "anthropic":
            # recusa de segurança: o servidor tenta outro modelo na mesma chamada
            r = self.client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
        else:
            r = self.client.messages.create(**kwargs)
        u = r.usage
        cost = getattr(u, "cost", None)
        if cost is None:
            cost = (u.input_tokens * self.price_in + u.output_tokens * self.price_out) / 1e6
        ans = Answer(job, None, time.time() - t0, u.input_tokens, u.output_tokens, float(cost))
        if r.stop_reason == "refusal":
            ans.error = "recusa do modelo"
            return ans
        if r.stop_reason == "max_tokens":
            ans.error = "resposta cortada (max_tokens)"
            return ans
        text = "".join(b.text for b in r.content if b.type == "text")
        try:
            ans.data = json.loads(text)
        except ValueError:
            ans.error = f"JSON inválido: {text[:120]}"
        return ans


class SonnetWorker:
    """Uma chamada por vez numa thread; fila curta (macro velho é descartado)."""

    def __init__(self, client: SonnetClient, sync: bool = False):
        self.client = client
        self.sync = sync
        self.jobs: queue.Queue[Job] = queue.Queue()
        self.results: queue.Queue[Answer] = queue.Queue()
        self.disabled_reason: str | None = None
        self.failures = 0
        self.backoff_until = 0.0
        if not sync:
            threading.Thread(target=self._loop, daemon=True).start()

    def submit(self, job: Job) -> bool:
        if self.disabled_reason or time.time() < self.backoff_until:
            return False
        if self.sync:
            self.results.put(self._call(job))
        else:
            self.jobs.put(job)
        return True

    def _loop(self) -> None:
        while True:
            job = self.jobs.get()
            self.results.put(self._call(job))

    def _call(self, job: Job) -> Answer:
        t0 = time.time()
        try:
            a = self.client.ask(job)
            self.failures = 0
            return a
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            self.disabled_reason = f"{type(e).__name__}: {e}"
            print(f"[sonnet] desativado: chave recusada (confira {self.client.key_env})")
            return Answer(job, None, time.time() - t0, error=self.disabled_reason)
        except anthropic.NotFoundError as e:
            self.disabled_reason = f"modelo não encontrado: {self.client.model} ({e})"
            print(f"[sonnet] desativado: {self.disabled_reason}")
            return Answer(job, None, time.time() - t0, error=self.disabled_reason)
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
            self.failures += 1
            if self.failures >= 3:
                self.backoff_until = time.time() + 120
                print(f"[sonnet] {self.failures} falhas seguidas; pausa de 2 min")
            return Answer(job, None, time.time() - t0, error=f"{type(e).__name__}: {e}")


class SonnetAdvisor:
    """Decide quando chamar o Sonnet e transforma as respostas em fala."""

    def __init__(self, cfg: dict, dd, worker: SonnetWorker, speech, log, elo=None, on_item=None):
        self.cfg, self.sc, self.dd = cfg, cfg["sonnet"], dd
        self.worker, self.speech, self.log = worker, speech, log
        self.elo = elo
        self.jungle = None                  # JungleTracker, ligado pelo app
        self.rotation = None                # RotationCoach, ligado pelo app
        self.on_item = on_item              # callback(rec, item_name, frase, motivo) do app
        self.on_plan = None                 # callback(texto) do app: mostra o plano na janela
        self.advisor_history = None         # histórico de recomendações do cálculo (BuildAdvisor.history)
        self.interval = float(self.sc.get("macro_intervalo_s", 210))
        self.started = False
        self.last_macro = 0.0
        self.turrets_lost = {"ORDER": 0, "CHAOS": 0}
        self.spoken: list[str] = []
        self.plan_text = ""
        self.stats = {"pedidos": 0, "respostas": 0, "faladas": 0, "custo": 0.0, "erros": 0}

    # ---------------- ciclo ----------------
    def tick(self, s: GameState, changes, raw: dict) -> None:
        for c in changes:
            if c.kind == "event" and c.data.get("EventName") == "TurretKilled":
                name = str(c.data.get("TurretKilled", ""))
                # nomes reais: Turret_TOrder_L0_P3_... / Turret_TChaos_L1_P2_... (time dono da torre)
                side = "ORDER" if "_TOrder_" in name else "CHAOS" if "_TChaos_" in name else None
                if side:
                    self.turrets_lost[side] += 1
        if not self.started and self.sc.get("plano_matchup", True):
            self.started = True
            opp = s.opponent
            self._submit(Job("matchup", s.game_time, self._matchup_payload(s, raw), MATCHUP_SCHEMA,
                             {"kit": self.dd.champion_kit(opp.champ_id) if opp else None,
                              "opp": opp.champ_name if opp else None}))
        if (self.sc.get("macro", True) and s.game_time >= 180 and not s.me.is_dead
                and s.game_time - self.last_macro >= self.interval):
            self.last_macro = s.game_time
            dead = [e.key for e in s.enemies if e.is_dead and e.respawn >= float(self.sc.get("morto_minimo_s", 15))]
            self._submit(Job("macro", s.game_time, self._macro_payload(s), MACRO_SCHEMA, {"mortos": dead}))
        while not self.worker.results.empty():
            self._handle(self.worker.results.get(), s)

    def request_item(self, rec, s: GameState, built: list[str]) -> bool:
        """Só candidatos perto do melhor no cálculo (padrão: 75%), pra a escolha não fugir dos números."""
        if not self.sc.get("item", True):
            return False
        floor = rec.ranking[0].per_1000 * float(self.sc.get("item_minimo_do_melhor", 0.75))
        prev_id = None
        if self.advisor_history and len(self.advisor_history) > 1:
            last = self.advisor_history[-2]
            prev_id = last.chosen_id or last.ranking[0].item.id
        # perto do melhor, ou o que foi recomendado da última vez (pode estar com componentes comprados)
        cands = [c for c in rec.ranking if c.per_1000 >= floor or c.item.id == prev_id or c.forced]
        if len(cands) < 2:
            return False
        names = [c.item.name for c in cands]
        return self._submit(Job("item", s.game_time, self._item_payload(rec, cands, s, built), item_schema(names),
                                {"rec": rec}))

    def _submit(self, job: Job) -> bool:
        ok = self.worker.submit(job)
        if ok:
            self.stats["pedidos"] += 1
        return ok

    # ---------------- respostas ----------------
    def _handle(self, a: Answer, s: GameState) -> None:
        from .rules import ALTA, NORMAL, Advice
        self.stats["respostas"] += 1
        self.stats["custo"] += a.cost
        rec = {"tipo": a.job.kind, "tempo_jogo": round(a.job.game_time, 1), "respondido_em": round(s.game_time, 1),
               "latencia_s": round(a.latency, 2), "tokens": [a.input_tokens, a.output_tokens],
               "custo_usd": round(a.cost, 6), "pedido": a.job.user, "resposta": a.data, "erro": a.error}
        if a.error or not a.data:
            self.stats["erros"] += 1
            rec["status"] = "erro"
            self.log(rec)
            return
        d = {k: (v.strip().strip("{}").strip() if isinstance(v, str) else v) for k, v in a.data.items()}
        status = "enviada_pra_fala"
        if a.job.kind == "matchup":
            dica = d.get("dica_matchup", "").strip().rstrip(".")
            frases = ([dica] if dica and len(dica.split()) <= 22 else []) + \
                     [f.strip().rstrip(".") for f in d.get("frases", []) if f.strip() and len(f.split()) <= 12][:2]
            self.plan_text = d.get("explicacao", "")
            if self.on_plan:
                self.on_plan(dica, frases[1:], self.plan_text, a.job.meta.get("kit"), a.job.meta.get("opp"))
            if frases:
                self.speech.push(Advice(". ".join(frases), NORMAL, "sonnet_plano", cooldown=1e9, ttl=120,
                                        created=s.game_time, reason="plano do matchup (Sonnet)"), s.game_time)
                self.spoken += frases
                self.stats["faladas"] += 1
            else:
                status = "sem frase utilizável"
        elif a.job.kind == "macro":
            frase = d.get("frase", "").strip().rstrip(".")
            late = s.game_time - a.job.game_time > float(self.sc.get("atraso_max_macro_s", 45))
            if late:
                status = "atrasada"
            elif s.me.is_dead:
                status = "jogador_morto"
            elif not frase or len(frase.split()) > 14:
                status = "frase longa demais"
            else:
                pr = ALTA if d.get("prioridade") == "alta" else NORMAL
                dead = set(a.job.meta.get("mortos", []))

                def still_true(st, dead=dead) -> bool:
                    # a fala conta com inimigo morto? então ele precisa continuar morto na hora de falar
                    alive_again = any(e.key in dead and not e.is_dead for e in st.enemies)
                    return not st.me.is_dead and not alive_again

                self.speech.push(Advice(frase, pr, "sonnet_macro", cooldown=120, ttl=15, created=s.game_time,
                                        reason=f"macro (Sonnet): {d.get('motivo', '')}",
                                        check=still_true), s.game_time)
                self.spoken.append(frase)
                self.stats["faladas"] += 1
        elif a.job.kind == "item":
            if self.on_item:
                status = self.on_item(a.job.meta["rec"], d.get("item"), d.get("frase", "").strip().rstrip("."),
                                      d.get("motivo", ""))
                if status == "enviada_pra_fala":
                    self.stats["faladas"] += 1
        rec["status"] = status
        self.log(rec)

    # ---------------- dados enviados ----------------
    def _player(self, p, me=False) -> dict:
        d = {"campeao": p.champ_name, "nivel": p.level, "abates_mortes_assist": f"{p.kills}/{p.deaths}/{p.assists}",
             "farm": p.cs, "valor_dos_itens": p.item_value, "feiticos": list(p.spell_names)}
        if p.position in ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"):
            d["rota"] = p.position
        # renasce logo: até a resposta chegar e ser falada ele já voltou, então conta como vivo
        if p.is_dead and p.respawn >= float(self.sc.get("morto_minimo_s", 15)):
            d["morto_renasce_em_s"] = round(p.respawn)
        return d

    def _matchup_payload(self, s: GameState, raw: dict) -> dict:
        runes = (raw.get("activePlayer") or {}).get("fullRunes") or {}
        rn = lambda k: (runes.get(k) or {}).get("displayName")
        prof = s.profile
        me = self._player(s.me)
        me.update({"rota": s.role, "runas": {"principal": rn("keystone"), "arvore_primaria": rn("primaryRuneTree"),
                                             "arvore_secundaria": rn("secondaryRuneTree")},
                   "perfil_do_coach": {"estilo": prof.get("estilo"), "niveis_fortes": prof.get("niveis_fortes"),
                                       "allin_a_partir_do_nivel": prof.get("allin_nivel")}})
        opp = s.opponent
        out = {
            "pedido": ("Plano para o começo da partida. A dica_matchup é a mais importante: diga a habilidade ou "
                       "passiva do oponente que mais decide a rota pela tecla, com uma descrição curta do que ela faz, "
                       "e o que fazer contra ela, de preferência usando a recarga (ex.: 'O E da Ahri, o beijo que "
                       "encanta, tem 12 segundos: troque quando ela errar'). Na explicação também: toda habilidade "
                       "citada começa pela tecla (Q, W, E, R ou passiva) e o que ela faz, nunca só o nome. "
                       "Se o jogador for caçador, o oponente é o caçador inimigo e a dica é sobre o duelo e as invasões. "
                       "Use só as descrições e recargas do kit enviado, que são do patch atual. As frases extras "
                       "falam de níveis fortes, all-in e primeiro objetivo. Considere os feitiços reais de cada um."),
            "modo_de_jogo": s.game_mode,
            "patch": self.dd.version,
            "jogador": me,
            "seu_kit": self.dd.champion_kit(s.me.champ_id, 220),
            "oponente_de_rota": self._player(opp) if opp else None,
            "kit_do_oponente": self.dd.champion_kit(opp.champ_id) if opp else None,
            "aliados": [self._player(p) for p in s.allies if p.key != s.me.key],
            "inimigos": [{**self._player(p), "kit": self._short_kit(p.champ_id)} for p in s.enemies
                         if not opp or p.key != opp.key],
        }
        if self.elo and self.elo.available:
            st = self.elo.lane(s.role, s.me.champ_id, opp.champ_id if opp else None)
            if st:
                out["estatisticas_elo_alto"] = st
            meta = {"itens_mais_feitos_pelo_seu_campeao": self.elo.items_for(s.role, s.me.champ_id, self.dd),
                    "campeoes_mais_jogados_na_rota": self.elo.meta_role(s.role, self.dd)}
            if opp:
                meta["oponente_na_rota"] = self.elo.lane(s.role, opp.champ_id, s.me.champ_id).get("campeao_na_rota")
                meta["itens_mais_feitos_pelo_oponente"] = self.elo.items_for(s.role, opp.champ_id, self.dd, 4)
            out["meta_elo_alto"] = {k: v for k, v in meta.items() if v}
        return out

    def _short_kit(self, cid: str | None) -> dict:
        """Kit resumido dos outros inimigos: nome de cada habilidade e a ultimate descrita."""
        kit = self.dd.champion_kit(cid, 160)
        if not kit:
            return {}
        out = {k: v["nome"] for k, v in kit.items() if k != "R"}
        if "R" in kit:
            out["R"] = f"{kit['R']['nome']}: {kit['R']['descricao']}"
        return out

    def _item_payload(self, rec, ranking, s: GameState, built: list[str]) -> dict:
        meta = self.elo.items_for(s.role, s.me.champ_id, self.dd, 12) if self.elo and self.elo.available else []
        usage = {m["item"]: m for m in meta}
        cands = []
        for i, c in enumerate(ranking, 1):
            cands.append({"posicao_no_calculo": i, "item": c.item.name, "custo": c.cost, "falta_de_ouro": c.remaining,
                          "pontos_por_1000_de_ouro": round(c.per_1000, 2), "ganho_de_dano_no_combo_pct": round(c.dmg_gain, 1),
                          "ganho_de_vida_efetiva_pct": round(c.ehp_gain, 1), "anticura": round(c.antiheal, 1),
                          "motivo_do_calculo": c.main_reason, "observacoes": c.notes,
                          "descricao_no_patch": self.dd.item_text(c.item.id, 380),
                          "ja_tem_componentes": c.remaining < c.cost,
                          "uso_em_elo_alto": ({"fracao_dos_jogadores_que_fazem": usage[c.item.name]["uso"],
                                               "popular": usage[c.item.name]["uso"] >= 0.5,
                                               "winrate_com_o_item": usage[c.item.name]["winrate"]}
                                              if c.item.name in usage else "raro (menos de 15% ou fora do top)")})
        prev = None
        if self.advisor_history:
            last = self.advisor_history[-2] if len(self.advisor_history) > 1 else None
            if last is not None:
                cid = last.chosen_id or last.ranking[0].item.id
                prev = next((c.item.name for c in last.ranking if c.item.id == cid), None)
        enemy_items = {}
        for e in s.enemies:
            for iid in e.items:
                if iid and self.dd.is_completed(iid) and not self.dd.is_boots(iid):
                    enemy_items.setdefault(self._item_name(iid), self.dd.item_text(iid, 220))
        return {
            "pedido": ("Escolha o próximo item entre os candidatos. O cálculo já simulou dano e defesa com os números "
                       "do patch; todos os candidatos estão perto do 1º. Regras, em ordem: "
                       "1) Se a recomendacao_anterior ainda está entre os candidatos, mantenha ela, principalmente se "
                       "ja_tem_componentes; trocar de ideia confunde o jogador e desperdiça ouro. Só troque por motivo "
                       "forte e novo (ameaça que apareceu agora). "
                       "2) Entre os outros, prefira o 1º do cálculo. Só escolha outro se algo fora do cálculo pesar: "
                       "passiva não modelada (ver observações), invisibilidade, cura forte, escudos, tipo de dano; ou se "
                       "o candidato tem popular=true em elo alto e o 1º é raro, sinal de passiva que o cálculo subestima. "
                       "Itens com estase contra assassino (ver observações) valem mais do que o cálculo mostra "
                       "quando o assassino está forte ou já te matou: aí prefira o item de estase. "
                       "Só cite elo alto (na frase ou no motivo) quando o item tem popular=true; nunca chame de maioria ou "
                       "de popular um item com popular=false. A frase não pode ter números."),
            "recomendacao_anterior": prev,
            "momento": "você morreu" if rec.reason == "morte" else "na base",
            "jogador": {**self._player(s.me), "rota": s.role,
                        "itens": [self._item_name(i) for i in s.me.items if i], "ouro": int(s.gold),
                        "itens_completos_feitos": built},
            "inimigos": [{**self._player(e), "itens": [self._item_name(i) for i in e.items if i]} for e in s.enemies],
            "perfil_de_dano_inimigo": {k: round(v, 2) for k, v in rec.profile.items()},
            "peso_dos_alvos": {k: round(v, 2) for k, v in rec.weights.items()},
            "candidatos": cands,
            "itens_dos_inimigos_no_patch": enemy_items,
            **({"itens_mais_feitos_em_elo_alto": meta} if (meta := self.elo.items_for(s.role, s.me.champ_id, self.dd)
                                                             if self.elo and self.elo.available else []) else {}),
        }

    def _macro_payload(self, s: GameState) -> dict:
        base = jev_summary(s)
        o = s.objectives
        mine = s.me.team
        theirs = "CHAOS" if mine == "ORDER" else "ORDER"
        out = {
            "pedido": ("Reavaliação do macro pros próximos 2 a 3 minutos: uma frase de até 10 palavras dizendo o que "
                       "fazer (objetivo, lado do mapa, agrupar ou dividir, farmar, proteger o carregador). "
                       "Pense em rotação: empurrar a wave antes de sair, que lado do mapa está livre e onde está o "
                       "jungler inimigo. "
                       "O coach já avisa sozinho ouro guardado, voltar à base, vida baixa e tempos de objetivo: "
                       "não fale disso; fale da estratégia de mapa. Não repita o que já foi dito, a menos que mudou."),
            **base,
            "aliados": [self._player(p) for p in s.allies],
            "inimigos": [self._player(p) for p in s.enemies],
            "torres_perdidas": {"nosso_time": self.turrets_lost[mine], "time_inimigo": self.turrets_lost[theirs]},
            **(self.rotation.structures(mine) if self.rotation else {}),
            "barao_ativo": None if o.baron_buff_team is None or s.game_time >= o.baron_buff_until
            else ("nosso time" if o.baron_buff_team == mine else "time inimigo"),
            "alma_com_dragoes": o.soul_at,
            "dragoes_que_faltam_pra_alma": {"nosso_time": max(0, o.soul_at - o.dragons.get(mine, 0)),
                                            "time_inimigo": max(0, o.soul_at - o.dragons.get(theirs, 0))},
            "ja_falado_pelo_coach": self.spoken[-4:],
        }
        if self.jungle:
            jg = self.jungle.describe(s)
            if jg:
                out["jungler_inimigo"] = jg
        if self.elo and self.elo.available:
            diff = sum(p.item_value for p in s.allies) - sum(p.item_value for p in s.enemies)
            team = self.elo.team_at(diff)
            if team:
                out["estatistica_time_elo_alto"] = team + " (valor dos itens; comparar só perto dos 15 min)"
        return out

    def _item_name(self, iid: int) -> str:
        it = self.dd.item(iid)
        return it["name"] if it else str(iid)
