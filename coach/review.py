"""Análise pós-partida automática: o que você fez certo e errado, com os números da partida.

Os fatos (mortes, abates, farm, ouro, voltas pra base, objetivos) saem da linha do tempo gravada
durante o jogo e dos eventos da Live API. O Sonnet, se estiver ligado, só escreve o texto de
"certo / errado / prioridades" em cima desses fatos; ele não inventa número."""
from __future__ import annotations

import json
from dataclasses import dataclass

from .logger import _mmss

GOLD_STUCK = 1300        # ouro no bolso que já compra um componente grande


@dataclass
class Tick:
    t: float
    hp: float            # % de vida
    gold: float
    level: int
    cs: int
    dead: bool
    items: int           # valor dos seus itens
    opp_level: int
    opp_cs: int
    opp_items: int
    mana: float = 1.0    # % de mana (1.0 pra quem não usa mana)


def _at(timeline: list[Tick], t: float) -> Tick | None:
    """Último ponto da linha do tempo até o instante t."""
    best = None
    for k in timeline:
        if k.t > t:
            break
        best = k
    return best


def facts(coach, s) -> dict:
    """Junta os fatos da partida num dicionário (vai pro texto e pro Sonnet)."""
    tl: list[Tick] = coach.timeline
    me, opp = s.me, s.opponent
    players = s.allies + s.enemies
    resolve = lambda n: coach.tracker.player_by_name(n, players)
    jg = coach.jungle.find_jungler(s.enemies) if coach.jungle else None
    spoken = _spoken(coach)

    deaths, kills = [], []
    streak = 0                    # seus abates desde a última morte
    for ev in coach.events:
        if ev.get("EventName") != "ChampionKill":
            continue
        t = float(ev.get("EventTime", 0))
        killer, victim = resolve(ev.get("KillerName")), resolve(ev.get("VictimName"))
        helpers = [resolve(a) for a in ev.get("Assisters") or []]
        if killer is not None and killer.key == me.key:
            streak += 1
        if victim is not None and victim.key == me.key:
            before = _at(tl, t - 10)
            last = _at(tl, t - 1)
            d = {"tempo": _mmss(t), "por": killer.champ_name if killer else ev.get("KillerName"),
                 "ajudaram": [h.champ_name for h in helpers if h],
                 "vida_10s_antes": round(before.hp * 100) if before else None,
                 "ouro_no_bolso": int(last.gold) if last else None}
            if last and opp:
                d["nivel_voce_x_oponente"] = f"{last.level} x {last.opp_level}"
                d["itens_voce_x_oponente"] = f"{last.items} x {last.opp_items}"
            d["coach_tinha_dito"] = [f"{_mmss(x['tempo_jogo'])} {x['texto']}" for x in spoken
                                     if t - 60 <= x["tempo_jogo"] < t - 10]
            d["coach_durante_a_luta"] = [f"{_mmss(x['tempo_jogo'])} {x['texto']}" for x in spoken
                                         if t - 10 <= x["tempo_jogo"] <= t]
            # o que o coach falou ANTES de a luta começar (nos últimos 10 s a luta já estava rolando)
            prior = [x["texto"] for x in spoken if t - 60 <= x["tempo_jogo"] < t - 10]
            d["diagnostico"] = _diagnose(d, last, before, killer, helpers, opp, jg, prior)
            if streak >= 3:
                d["diagnostico"].insert(0, f"morreu valendo recompensa ({streak} abates seguidos): entregou ouro extra")
            elif last and s.enemies and last.items - last.opp_items >= 2500 and t >= 18 * 60 and len(helpers) >= 2:
                d["diagnostico"].insert(0, "muito à frente e pego por vários: provavelmente estava sozinho")
            streak = 0
            deaths.append(d)
        elif killer is not None and killer.key == me.key or any(h is not None and h.key == me.key for h in helpers):
            lane = victim.position if victim and victim.position else "?"
            kills.append({"tempo": _mmss(t), "tipo": "abate" if killer and killer.key == me.key else "assistência",
                          "em": victim.champ_name if victim else ev.get("VictimName"), "rota_da_vitima": lane})

    out = {
        "campeao": me.champ_name, "rota": s.role, "duracao": _mmss(s.game_time),
        "placar": f"{me.kills}/{me.deaths}/{me.assists}",
        "abates_dos_times": {"seu": sum(p.kills for p in s.allies), "inimigo": sum(p.kills for p in s.enemies)},
        "oponente": opp.champ_name if opp else None,
        "mortes": deaths, "abates_e_assistencias": kills,
        "tempo_morto_s": int(sum(1 for k in tl if k.dead)),
    }
    # farm e nível
    curve = {}
    for m in (5, 10, 15):
        k = _at(tl, m * 60)
        if k and s.game_time >= m * 60:
            curve[f"{m}min"] = {"farm": k.cs, "farm_oponente": k.opp_cs, "nivel": k.level, "nivel_oponente": k.opp_level}
    out["farm_e_nivel"] = curve
    first = next((k for k in tl if k.cs > 0), None)
    if first is not None:
        out["primeira_tropa_registrada"] = _mmss(first.t) + " (o farm da API sobe de 10 em 10: é quando chegou a 10)"
    no_mana = [k for k in tl if k.mana < 0.15 and not k.dead and k.t < 14 * 60]
    if len(no_mana) >= 30:
        out["segundos_sem_mana_na_rota"] = len(no_mana)
        out["primeiro_momento_sem_mana"] = _mmss(no_mana[0].t)
    behind = [k for k in tl if k.opp_level > k.level and not k.dead]
    if behind:
        out["segundos_com_oponente_um_nivel_na_frente"] = len(behind)
    stuck = []
    for a, b in _flat_farm(tl):
        stuck.append(f"{_mmss(a)}–{_mmss(b)}")
    if stuck:
        out["farm_parado_mais_de_90s"] = stuck
    if coach.elo and coach.elo.available and s.role in ("TOP", "MIDDLE", "BOTTOM"):
        ref = coach.elo.lane(s.role, me.champ_id, None).get("farm_aos_10")
        if ref:
            out["farm_aos_10_elo_alto"] = {"mediana": ref["mediana"], "top_25": ref["p75"]}
    # ouro parado
    out["ouro_parado"] = [f"{_mmss(a)}–{_mmss(b)} ({int(b - a)} s com {GOLD_STUCK}+ de ouro)" for a, b in _gold_stuck(tl)]
    out["compras"] = coach.purchases
    # objetivos
    if coach.rotation:
        rec = coach.rotation.objectives
        out["objetivos"] = {
            "do_seu_time": len([r for r in rec if r.ours]),
            "voce_participou": [f"{r.name} {_mmss(r.t)}" for r in rec if r.ours and r.me_in],
            "seu_time_fez_sem_voce_e_voce_estava_vivo": [f"{r.name} {_mmss(r.t)}" for r in rec
                                                         if r.ours and not r.me_in and r.me_alive],
            "inimigo_fez_com_voce_vivo": [f"{r.name} {_mmss(r.t)}" + _trade(coach, s, r.t)
                                          for r in rec if not r.ours and r.me_alive],
            "estruturas_com_voce": coach.rotation.my_structures,
        }
    out["numeros"] = _numbers(coach, s, out, tl, spoken)
    return out


def _numbers(coach, s, f: dict, tl: list[Tick], spoken: list[dict]) -> dict:
    """Números da partida em formato fixo, pro relatório de várias partidas (coach/report.py)."""
    end = next((e for e in coach.events if e.get("EventName") == "GameEnd"), None)
    ten = f["farm_e_nivel"].get("10min") or {}
    death_t = [float(e.get("EventTime", 0)) for e in coach.events if e.get("EventName") == "ChampionKill"
               and (v := coach.tracker.player_by_name(e.get("VictimName"), s.allies + s.enemies)) is not None
               and v.key == s.me.key]
    kinds = {"gank": "gank", "varios": "pego por vários", "ouro_no_bolso": "ouro sem gastar",
             "pouca_vida": "entrou na luta com", "vida_cheia_em_desvantagem": "na frente (nível ou itens)",
             "recompensa_ou_sozinho": ("recompensa", "muito à frente")}
    tipos = {}
    for k, pat in kinds.items():
        pats = pat if isinstance(pat, tuple) else (pat,)
        tipos[k] = sum(1 for d in f["mortes"] if any(p in x for x in d["diagnostico"] for p in pats))
    obj = f.get("objetivos") or {}
    return {
        "pasta": coach.log.dir.name, "modo": s.game_mode, "campeao_id": s.me.champ_id, "campeao": s.me.champ_name,
        "rota": s.role, "oponente": f["oponente"], "duracao_s": int(s.game_time),
        "resultado": {"Win": "vitória", "Lose": "derrota"}.get(str(end.get("Result")) if end else "", "desconhecido"),
        "abates": s.me.kills, "mortes": s.me.deaths, "assistencias": s.me.assists,
        "farm_10": ten.get("farm"), "farm_10_oponente": ten.get("farm_oponente"),
        "tempo_morto_s": f["tempo_morto_s"],
        "ouro_parado_s": sum(b - a for a, b in _gold_stuck(tl)),
        "sem_mana_s": f.get("segundos_sem_mana_na_rota", 0),
        "objetivos_do_time": obj.get("do_seu_time", 0),
        "objetivos_com_voce": len(obj.get("voce_participou", [])),
        "objetivos_do_time_sem_voce": len(obj.get("seu_time_fez_sem_voce_e_voce_estava_vivo", [])),
        "objetivos_inimigos_sem_troca": len([x for x in obj.get("inimigo_fez_com_voce_vivo", []) if "troca:" not in x]),
        "mortes_por_tipo": tipos,
        "mortes_em_s": death_t,
        "compras_em_s": list(coach.purchase_times),
    }


def _trade(coach, s, t: float) -> str:
    """Objetivo inimigo trocado: abates ou torres do seu time 30 s antes/depois, em outro lugar do mapa.
    Aí não é (só) rotação perdida; a pergunta é se você aproveitou a troca."""
    players = s.allies + s.enemies
    team = s.me.team
    kills = towers = 0
    for ev in coach.events:
        et = float(ev.get("EventTime", 0))
        if abs(et - t) > 30:
            continue
        killer = coach.tracker.player_by_name(ev.get("KillerName"), players)
        if ev.get("EventName") == "ChampionKill" and killer is not None and killer.team == team:
            kills += 1
        elif ev.get("EventName") == "TurretKilled" and "_TOrder_" in str(ev.get("TurretKilled")) and team == "CHAOS" \
                or ev.get("EventName") == "TurretKilled" and "_TChaos_" in str(ev.get("TurretKilled")) and team == "ORDER":
            towers += 1
    if kills < 2 and not towers:      # um abate solto não paga um objetivo
        return ""
    parts = ([f"{kills} abate(s)"] if kills else []) + ([f"{towers} torre(s)"] if towers else [])
    return f" (troca: seu time fez {' e '.join(parts)} ao mesmo tempo)"


def _spoken(coach) -> list[dict]:
    path = coach.log.dir / "conselhos.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            x = json.loads(line)
        except ValueError:
            continue
        if x.get("status") == "falado":
            rows.append(x)
    return rows


def _diagnose(d, last: Tick | None, before: Tick | None, killer, helpers, opp, jg, prior: list[str]) -> list[str]:
    out = []
    many = len([h for h in helpers if h]) >= 2
    if jg is not None and (killer is not None and killer.key == jg.key or any(h is not None and h.key == jg.key for h in helpers)):
        out.append("o jungler inimigo participou (gank)")
    if many:
        out.append("pego por vários ao mesmo tempo")
    if last and last.gold >= GOLD_STUCK:
        out.append(f"morreu com {int(last.gold)} de ouro sem gastar: dava pra ter voltado antes")
    if before and before.hp < 0.5:
        out.append(f"entrou na luta com {before.hp:.0%} de vida")
    if before and before.hp >= 0.8 and last and opp and killer is not None and killer.key == opp.key and not many:
        if last.opp_level > last.level or last.opp_items > last.items + 300:
            out.append("perdeu luta de vida cheia contra o oponente que estava na frente (nível ou itens)")
        else:
            out.append("perdeu luta de vida cheia contra o oponente parelho")
    if any("recue" in x.lower() or "jogue seguro" in x.lower() for x in prior):
        out.append("o coach tinha mandado jogar seguro antes da luta")
    if any("volte" in x.lower() for x in prior):
        out.append("o coach tinha mandado voltar pra base antes da luta")
    return out


def _flat_farm(tl: list[Tick]) -> list[tuple[float, float]]:
    """Trechos de mais de 90 s vivo e sem o farm subir (o creepScore sobe de 10 em 10)."""
    out, start, cs = [], None, None
    for k in tl:
        if k.t < 120:
            continue
        if k.dead or (cs is not None and k.cs != cs):
            if start is not None and k.t - start > 90:
                out.append((start, k.t))
            start, cs = None, None
        if not k.dead and cs is None:
            start, cs = k.t, k.cs
    return out


def _gold_stuck(tl: list[Tick]) -> list[tuple[float, float]]:
    out, start = [], None
    for k in tl:
        if k.gold >= GOLD_STUCK and not k.dead:
            start = k.t if start is None else start
        else:
            if start is not None and k.t - start > 45:
                out.append((start, k.t))
            start = None
    return out


def priorities(f: dict) -> list[str]:
    """Regras simples, em ordem de peso, a partir dos fatos (servem mesmo sem Sonnet)."""
    out = []
    gold_deaths = [d for d in f["mortes"] if any("ouro sem gastar" in x for x in d["diagnostico"])]
    if gold_deaths:
        out.append(f"Volte pra base depois de abate ou objetivo com {GOLD_STUCK}+ de ouro "
                   f"({len(gold_deaths)} morte(s) com ouro no bolso).")
    ten = f["farm_e_nivel"].get("10min")
    ref = f.get("farm_aos_10_elo_alto")
    if ten and f["rota"] in ("TOP", "MIDDLE", "BOTTOM") and ten["farm"] < 70:
        alvo = f" (Mestre+: mediana {ref['mediana']})" if ref else ""
        out.append(f"Farm: {ten['farm']} aos 10 min; mire em 70 ou mais{alvo}.")
    if f.get("segundos_sem_mana_na_rota", 0) >= 60:
        out.append(f"Mana: {f['segundos_sem_mana_na_rota']} s sem mana na rota (desde {f['primeiro_momento_sem_mana']}); "
                   "use as habilidades pra trocar ou garantir tropa, não em toda wave.")
    behind_fights = [d for d in f["mortes"] if any("na frente" in x for x in d["diagnostico"])]
    if behind_fights:
        out.append("Não force luta de vida cheia contra quem está na sua frente em nível ou itens.")
    missed = (f.get("objetivos") or {}).get("seu_time_fez_sem_voce_e_voce_estava_vivo") or []
    lost = (f.get("objetivos") or {}).get("inimigo_fez_com_voce_vivo") or []
    traded = [x for x in lost if "troca:" in x]
    lost = [x for x in lost if "troca:" not in x]
    if missed or lost:
        out.append(f"Rotação: {len(missed)} objetivo(s) do time sem você e {len(lost)} do inimigo com você vivo; "
                   "empurre a wave 60 s antes e vá junto.")
    if traded:
        out.append("Troca de mapa: quando o time luta longe e o inimigo faz objetivo, não fique parado esperando; "
                   "empurre sua wave, pegue placa ou campos do jungler inimigo, ou junte no time pra virar torre.")
    if f.get("ouro_parado"):
        out.append("Ouro parado: " + ", ".join(f["ouro_parado"][:3]) + ".")
    rich = [d for d in f["mortes"] if any("recompensa" in x or "muito à frente" in x for x in d["diagnostico"])]
    if rich:
        out.insert(0, f"Na frente, ande com o time: {len(rich)} morte(s) valendo recompensa ou sozinho em vantagem.")
    gank = [d for d in f["mortes"] if any("gank" in x for x in d["diagnostico"])]
    if gank:
        out.append("Mortes pra gank: jogue atrás da wave quando o jungler inimigo some do mapa.")
    return out[:4]


SONNET_SCHEMA = {
    "type": "object",
    "properties": {
        "certo": {"type": "array", "items": {"type": "string"}, "description": "2 a 4 acertos, uma frase cada"},
        "errado": {"type": "array", "items": {"type": "string"}, "description": "2 a 4 erros, uma frase cada"},
        "prioridades": {"type": "array", "items": {"type": "string"}, "description": "até 3 coisas pra próxima partida"},
    },
    "required": ["certo", "errado", "prioridades"],
    "additionalProperties": False,
}


def text(f: dict, coach_text: dict | None, extra: list[str]) -> str:
    L = [f"ANÁLISE DA PARTIDA — {f['campeao']} ({f['rota']}) contra {f['oponente'] or '?'} — {f['duracao']}",
         f"Placar {f['placar']} | abates dos times: seu {f['abates_dos_times']['seu']} x {f['abates_dos_times']['inimigo']} inimigo"
         f" | tempo morto ~{f['tempo_morto_s']} s", ""]
    if coach_text:
        L.append("O QUE VOCÊ FEZ CERTO")
        L += [f"  + {x}" for x in coach_text.get("certo", [])]
        L.append("O QUE VOCÊ FEZ ERRADO")
        L += [f"  - {x}" for x in coach_text.get("errado", [])]
        L.append("PRA PRÓXIMA")
        L += [f"  {i}. {x}" for i, x in enumerate(coach_text.get("prioridades", []), 1)]
        L.append("")
    else:
        L.append("PRA PRÓXIMA")
        L += [f"  {i}. {x}" for i, x in enumerate(priorities(f), 1)]
        L.append("")
    L.append("SUAS MORTES")
    if not f["mortes"]:
        L.append("  nenhuma")
    for d in f["mortes"]:
        L.append(f"  {d['tempo']} pra {d['por']}" + (f" + {', '.join(d['ajudaram'])}" if d["ajudaram"] else "")
                 + f" | vida 10 s antes {d['vida_10s_antes']}% | ouro no bolso {d['ouro_no_bolso']}"
                 + (f" | nível {d['nivel_voce_x_oponente']} | itens {d['itens_voce_x_oponente']}" if "nivel_voce_x_oponente" in d else ""))
        for x in d["diagnostico"]:
            L.append(f"      • {x}")
        if d["coach_tinha_dito"]:
            L.append("      coach antes da luta: " + " / ".join(d["coach_tinha_dito"][-3:]))
        if d["coach_durante_a_luta"]:
            L.append("      coach durante a luta: " + " / ".join(d["coach_durante_a_luta"]))
    L.append("SEUS ABATES E ASSISTÊNCIAS")
    L += [f"  {k['tempo']} {k['tipo']} em {k['em']} (rota {k['rota_da_vitima']})" for k in f["abates_e_assistencias"]] or ["  nenhum"]
    L.append("FARM E NÍVEL")
    for m, v in f["farm_e_nivel"].items():
        L.append(f"  {m}: farm {v['farm']} x {v['farm_oponente']} | nível {v['nivel']} x {v['nivel_oponente']}")
    if f.get("farm_aos_10_elo_alto"):
        r = f["farm_aos_10_elo_alto"]
        L.append(f"  Mestre+ aos 10 min: mediana {r['mediana']}, 25% melhores {r['top_25']}+")
    if f.get("farm_parado_mais_de_90s"):
        L.append("  farm parado (vivo, mais de 90 s): " + ", ".join(f["farm_parado_mais_de_90s"]))
    if f.get("segundos_com_oponente_um_nivel_na_frente"):
        L.append(f"  oponente com nível acima do seu por ~{f['segundos_com_oponente_um_nivel_na_frente']} s")
    L.append("OURO E VOLTAS PRA BASE")
    L += [f"  {c}" for c in f["compras"]] or ["  nenhuma compra registrada"]
    if f["ouro_parado"]:
        L.append("  ouro parado: " + ", ".join(f["ouro_parado"]))
    if f.get("objetivos"):
        o = f["objetivos"]
        L.append("OBJETIVOS")
        L.append(f"  do seu time: {o['do_seu_time']}; você participou: {', '.join(o['voce_participou']) or 'nenhum'}")
        if o["seu_time_fez_sem_voce_e_voce_estava_vivo"]:
            L.append(f"  seu time fez sem você (você estava vivo): {', '.join(o['seu_time_fez_sem_voce_e_voce_estava_vivo'])}")
        if o["inimigo_fez_com_voce_vivo"]:
            L.append(f"  inimigo fez com você vivo: {', '.join(o['inimigo_fez_com_voce_vivo'])}")
    L += [""] + extra
    L.append("")
    L.append("Limite: a API não mostra posição nem habilidades acertadas; o diagnóstico usa vida, ouro, nível, itens e eventos.")
    return "\n".join(L)


def build(coach, s) -> str:
    """Texto da análise. Os dados ficam em coach.review_data (vira analise.json pro relatório)."""
    f = facts(coach, s)
    coach_text = None
    client = coach.sonnet_client
    if client is not None and coach.cfg["analise"].get("sonnet", True):
        from .sonnet import Job
        job = Job("analise", s.game_time, {
            "pedido": ("Análise pós-partida pro jogador, em português do Brasil. É texto pra ler, não fala: cada item "
                       "pode ter até 30 palavras. Com base só nos fatos, escreva o que ele "
                       "fez certo, o que fez errado e até 3 prioridades pra próxima partida. Seja específico (cite o minuto "
                       "e os números dos fatos), direto e construtivo. Não invente nada que não esteja nos fatos; a API não "
                       "mostra posição nem habilidades acertadas, nem ping ou chat do time. Cada erro precisa estar "
                       "sustentado pelos fatos: use os diagnósticos das mortes e as prioridades calculadas; não diga que "
                       "ele devia ter voltado à base por ouro se ele tinha menos de 1300 no bolso. Objetivo inimigo marcado como "
                       "troca não é erro de rotação do jogador: o ponto é aproveitar a troca (farm, placa, torre, campos). "
                       "Ao citar habilidade, use a tecla e o que ela faz."),
            # falas que tocaram com a luta já rolando não vão pro modelo: ele lia como "ignorou o aviso"
            "fatos": {**f, "mortes": [{k: v for k, v in d.items() if k != "coach_durante_a_luta"} for d in f["mortes"]]},
            "prioridades_calculadas": priorities(f),
        }, SONNET_SCHEMA)
        try:
            a = client.ask(job)
            if a.data and not a.error:
                coach_text = a.data
                coach.log.text(f"[análise] Sonnet: {a.latency:.1f}s, US$ {a.cost:.4f}")
        except Exception as e:      # sem Sonnet a análise sai só com as regras
            coach.log.text(f"[análise] Sonnet falhou: {e}")
    coach.review_data = {"versao": 1, "fatos": f, "prioridades": priorities(f), "texto_sonnet": coach_text}
    return text(f, coach_text, [])
