"""Relatório de várias partidas: junta o analise.json de cada partida real (logs/<partida>/) e mostra
evolução, tipos de morte, hábitos que se repetem e se você seguiu o coach.

Partidas antigas sem analise.json são reprocessadas a partir do snapshots.jsonl.gz (sem voz, Jev
nem Sonnet, então não custa nada). Resultado que a API não mandou (sem evento GameEnd) pode ser
informado num arquivo resultado.txt na pasta da partida, com "vitória" ou "derrota". Partida que não
diz nada sobre como você joga (troll, afk no seu time) leva "(troll)" ou "(excluir)" no resultado.txt:
ela continua na tabela, mas fica fora das médias e dos hábitos."""
from __future__ import annotations

import copy
import json
import shutil
from collections import Counter, defaultdict
from pathlib import Path

from .config import ROOT

LOGS = ROOT / "logs"
VERSION = 1

HABITS = [   # (palavra-chave na prioridade da partida, nome do hábito)
    ("ande com o time", "morrer sozinho quando está na frente"),
    ("Volte pra base", "não voltar pra base com ouro"),
    ("Farm:", "farm baixo aos 10 min"),
    ("Não force luta", "lutar de vida cheia contra quem está na frente"),
    ("Rotação", "chegar atrasado nos objetivos"),
    ("Troca de mapa", "ficar parado quando o time troca objetivo"),
    ("Ouro parado", "ouro parado no bolso"),
    ("gank", "morrer pra gank"),
    ("Mana", "ficar sem mana na rota"),
]


def game_dirs() -> list[Path]:
    return sorted(d for d in LOGS.iterdir()
                  if d.is_dir() and not d.name.endswith("_replay") and (d / "snapshots.jsonl.gz").exists())


def reprocess(game: Path, ctx: tuple, log=print) -> bool:
    """Toca os snapshots da partida em silêncio e grava o analise.json na pasta original."""
    from .app import Coach
    from .collector import replay

    cfg, champs, dd, voice, advisor, _jev, _sonnet, elo = ctx
    cfg = copy.deepcopy(cfg)
    cfg["analise"]["sonnet"] = False
    cfg["build"]["janela"] = False
    coach = Coach(cfg, champs, dd, voice, replay_mode=True, advisor=advisor, elo=elo)
    coach._write_build_file = lambda text: None   # não mexe na janela (pode ter uma partida ao vivo usando)
    try:
        for data in replay(game / "snapshots.jsonl.gz", speed=0):
            coach.feed(data)
            if coach.ended:
                break
    except (EOFError, OSError, ValueError) as e:   # partida ainda gravando ou arquivo cortado
        log(f"  {game.name}: arquivo incompleto ({type(e).__name__}), pulando (partida em andamento?)")
        if coach.active and coach.log.dir.name.endswith("_replay"):
            shutil.rmtree(coach.log.dir, ignore_errors=True)
        return False
    if coach.active:
        coach.end("reprocessado")
    tmp = coach.log.dir
    data = coach.review_data
    if tmp != game and tmp.name.endswith("_replay"):
        shutil.rmtree(tmp, ignore_errors=True)
    if not data:
        log(f"  {game.name}: partida curta demais, sem análise")
        return False
    data["fatos"]["numeros"]["pasta"] = game.name
    (game / "analise.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"  {game.name}: análise gerada")
    return True


def load(log=print) -> list[dict]:
    games = []
    ctx = None
    for d in game_dirs():
        path = d / "analise.json"
        if not path.exists() or json.loads(path.read_text(encoding="utf-8")).get("versao", 0) < VERSION:
            if ctx is None:   # carrega Data Dragon, CommunityDragon e elo alto uma vez só
                from .app import _setup
                ctx = _setup(no_voice=True, no_jev=True, no_sonnet=True)
            log(f"Reprocessando {d.name} (sem custo) ...")
            if not reprocess(d, ctx, log):
                continue
        data = json.loads(path.read_text(encoding="utf-8"))
        n = data["fatos"]["numeros"]
        n["pasta"] = d.name
        n.update(compliance(d, data["fatos"]))
        override = d / "resultado.txt"
        if override.exists():
            raw = override.read_text(encoding="utf-8").strip().lower()
            n["excluida"] = "troll" in raw or "excluir" in raw
            n["resultado"] = raw.split("(")[0].strip() or n["resultado"]
            if n["excluida"]:
                n["motivo_exclusao"] = raw[raw.find("(") + 1:].rstrip(")") if "(" in raw else "excluída"
        else:
            n["excluida"] = False
        if n["duracao_s"] < 300:     # remake ou queda: não diz nada sobre como você joga
            log(f"  {d.name}: {n['duracao_s']} s, fora do relatório (remake?)")
            continue
        games.append(data)
    return games


def _mmss_to_s(x: str) -> float:
    m, s = x.split(":")[:2]
    return int(m) * 60 + int(s[:2])


def compliance(game_dir: Path, facts: dict) -> dict:
    """O coach mandou e você fez? Só avisos estratégicos: "vida crítica, recue" toca no meio da luta,
    um segundo antes da morte, e não é conselho ignorado."""
    spoken = []
    path = game_dir / "conselhos.jsonl"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                x = json.loads(line)
            except ValueError:
                continue
            if x.get("status") == "falado":
                spoken.append(x)
    n = facts["numeros"]
    deaths = n.get("mortes_em_s") or [_mmss_to_s(d["tempo"]) for d in facts["mortes"]]
    buys = n.get("compras_em_s") or [_mmss_to_s(c.split()[0]) for c in facts.get("compras", [])]
    back = [x["tempo_jogo"] for x in spoken if "volte" in x["texto"].lower()]
    safe = [x["tempo_jogo"] for x in spoken if not str(x.get("chave", "")).startswith("hp_")
            and any(w in x["texto"].lower() for w in ("recue", "jogue seguro", "cuidado", "não ande sozinho",
                                                      "lute junto"))]
    return {"coach_mandou_voltar": len(back),
            "voltou_ate_60s_depois": sum(1 for t in back if any(t < p <= t + 60 for p in buys)),
            "coach_mandou_jogar_seguro": len(safe),
            "morreu_ate_45s_depois": sum(1 for t in safe if any(t < d <= t + 45 for d in deaths))}


def _avg(xs) -> float | None:
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _fmt(x, nd=0, suffix="") -> str:
    return "-" if x is None else f"{x:.{nd}f}{suffix}"


def aggregate(games: list[dict]) -> dict:
    nums = [g["fatos"]["numeros"] for g in games]
    lanes = [n for n in nums if n["rota"] in ("TOP", "MIDDLE", "BOTTOM")]
    deaths_by_kind = Counter()
    for n in nums:
        deaths_by_kind.update(n["mortes_por_tipo"])
    habits = Counter()
    for g in games:
        for p in g["prioridades"]:
            for key, name in HABITS:
                if key.lower() in p.lower():
                    habits[name] += 1
                    break
    per_champ = defaultdict(list)
    for n in nums:
        per_champ[n["campeao"]].append(n)
    res = Counter(n["resultado"] for n in nums)
    half = len(nums) // 2
    trend = None
    if len(nums) >= 4:
        a, b = nums[:half], nums[half:]
        trend = {
            "farm_10": (_avg(n["farm_10"] for n in a if n in lanes), _avg(n["farm_10"] for n in b if n in lanes)),
            "mortes": (_avg(n["mortes"] for n in a), _avg(n["mortes"] for n in b)),
            "ouro_parado_s": (_avg(n["ouro_parado_s"] for n in a), _avg(n["ouro_parado_s"] for n in b)),
        }
    obj_team = sum(n["objetivos_do_time"] for n in nums)
    return {
        "partidas": len(nums), "resultados": dict(res),
        "farm_10_medio_rotas": _avg(n["farm_10"] for n in lanes),
        "mortes_por_partida": _avg(n["mortes"] for n in nums),
        "tempo_morto_medio_s": _avg(n["tempo_morto_s"] for n in nums),
        "ouro_parado_medio_s": _avg(n["ouro_parado_s"] for n in nums),
        "mortes_por_tipo": dict(deaths_by_kind), "total_de_mortes": sum(n["mortes"] for n in nums),
        "participacao_em_objetivos": f"{sum(n['objetivos_com_voce'] for n in nums)} de {obj_team}",
        "objetivos_inimigos_sem_troca_com_voce_vivo": sum(n["objetivos_inimigos_sem_troca"] for n in nums),
        "habitos_que_se_repetem": habits.most_common(),
        "coach_mandou_voltar": f"voltou em até 60 s em {sum(n['voltou_ate_60s_depois'] for n in nums)} de "
                               f"{sum(n['coach_mandou_voltar'] for n in nums)} vezes",
        "coach_mandou_jogar_seguro": f"morreu em até 45 s em {sum(n['morreu_ate_45s_depois'] for n in nums)} de "
                                     f"{sum(n['coach_mandou_jogar_seguro'] for n in nums)} avisos (sem contar 'vida crítica', "
                                     "que toca no meio da luta)",
        "por_campeao": {c: {"partidas": len(v), "vitorias": sum(n["resultado"] == "vitória" for n in v),
                            "farm_10_medio": _avg(n["farm_10"] for n in v), "mortes_medias": _avg(n["mortes"] for n in v),
                            "sem_mana_medio_s": _avg(n["sem_mana_s"] for n in v)} for c, v in per_champ.items()},
        "tendencia_primeira_x_segunda_metade": trend,
    }


DEATH_NAMES = {"gank": "gank do jungler", "varios": "pego por vários", "ouro_no_bolso": "com ouro no bolso",
               "pouca_vida": "entrou com pouca vida", "vida_cheia_em_desvantagem": "luta de vida cheia em desvantagem",
               "recompensa_ou_sozinho": "valendo recompensa ou sozinho na frente"}

SONNET_SCHEMA = {
    "type": "object",
    "properties": {
        "resumo": {"type": "string", "description": "2 a 3 frases sobre o momento do jogador"},
        "habitos": {"type": "array", "items": {"type": "string"},
                    "description": "os 3 hábitos que mais custam partidas, cada um com o número que mostra isso"},
        "melhorou": {"type": "array", "items": {"type": "string"}, "description": "o que melhorou, se os números mostram"},
        "plano": {"type": "array", "items": {"type": "string"},
                  "description": "até 3 tarefas concretas pras próximas partidas (uma por hábito)"},
    },
    "required": ["resumo", "habitos", "melhorou", "plano"],
    "additionalProperties": False,
}


def build_text(games: list[dict], agg: dict, coach_text: dict | None) -> str:
    nums = [g["fatos"]["numeros"] for g in games]
    r = agg["resultados"]
    extra = f", {agg['partidas']} na análise" if agg["partidas"] != len(nums) else ""
    L = [f"RELATÓRIO DE {len(nums)} PARTIDAS ({nums[0]['pasta'][:10]} a {nums[-1]['pasta'][:10]}{extra})",
         f"Resultados: {r.get('vitória', 0)} vitórias, {r.get('derrota', 0)} derrotas"
         + (f", {r['desconhecido']} sem resultado (crie resultado.txt na pasta da partida)" if r.get("desconhecido") else ""),
         ]
    if agg.get("excluidas"):
        L.append("Fora das médias e dos hábitos: " + "; ".join(agg["excluidas"]))
    L.append("")
    if coach_text:
        L += ["RESUMO", "  " + coach_text["resumo"], "", "HÁBITOS QUE MAIS CUSTAM PARTIDAS"]
        L += [f"  {i}. {h}" for i, h in enumerate(coach_text["habitos"], 1)]
        if coach_text.get("melhorou"):
            L += ["O QUE MELHOROU"] + [f"  + {x}" for x in coach_text["melhorou"]]
        L += ["PLANO PRAS PRÓXIMAS PARTIDAS"] + [f"  {i}. {x}" for i, x in enumerate(coach_text["plano"], 1)] + [""]
    L.append("HÁBITOS (quantas partidas tiveram cada um)")
    L += [f"  {name}: {n} de {agg['partidas']}" for name, n in agg["habitos_que_se_repetem"]] or ["  nenhum"]
    L.append("")
    L.append(f"MORTES: {agg['total_de_mortes']} no total, {_fmt(agg['mortes_por_partida'], 1)} por partida, "
             f"~{_fmt(agg['tempo_morto_medio_s'])} s morto por partida")
    for k, v in sorted(agg["mortes_por_tipo"].items(), key=lambda x: -x[1]):
        if v:
            L.append(f"  {DEATH_NAMES.get(k, k)}: {v}")
    L.append("  (uma morte pode ter mais de um tipo)")
    L.append("")
    L.append(f"FARM aos 10 min (partidas de rota): média {_fmt(agg['farm_10_medio_rotas'])}")
    L.append(f"OURO PARADO (1300+ por mais de 45 s): média {_fmt(agg['ouro_parado_medio_s'])} s por partida")
    L.append(f"OBJETIVOS: participou de {agg['participacao_em_objetivos']} do time; "
             f"{agg['objetivos_inimigos_sem_troca_com_voce_vivo']} objetivos inimigos sem troca com você vivo")
    L.append(f"COACH: mandou voltar e você {agg['coach_mandou_voltar']}; "
             f"mandou jogar seguro (antes da luta) e você {agg['coach_mandou_jogar_seguro']}")
    if agg["tendencia_primeira_x_segunda_metade"]:
        t = agg["tendencia_primeira_x_segunda_metade"]
        L.append("TENDÊNCIA (primeira metade x segunda metade das partidas): "
                 f"farm aos 10 {_fmt(t['farm_10'][0])} → {_fmt(t['farm_10'][1])}, "
                 f"mortes {_fmt(t['mortes'][0], 1)} → {_fmt(t['mortes'][1], 1)}, "
                 f"ouro parado {_fmt(t['ouro_parado_s'][0])} → {_fmt(t['ouro_parado_s'][1])} s")
    L.append("")
    L.append("POR CAMPEÃO")
    for c, v in agg["por_campeao"].items():
        L.append(f"  {c}: {v['partidas']} partida(s), {v['vitorias']} vitória(s), farm aos 10 {_fmt(v['farm_10_medio'])}, "
                 f"{_fmt(v['mortes_medias'], 1)} mortes" + (f", {_fmt(v['sem_mana_medio_s'])} s sem mana" if v["sem_mana_medio_s"] else ""))
    L.append("")
    L.append("PARTIDA A PARTIDA")
    L.append(f"  {'data':16} {'campeão':9} {'contra':10} {'result.':9} {'placar':8} {'farm10':>6} {'morto':>6} {'ouro parado':>11} {'objetivos':>9}")
    for n in nums:
        farm = "-" if n["farm_10"] is None else f"{n['farm_10']}x{n['farm_10_oponente']}"
        res = (n["resultado"][:7] + "*") if n.get("excluida") else n["resultado"][:9]
        L.append(f"  {n['pasta'][:16]:16} {n['campeao'][:9]:9} {(n['oponente'] or '-')[:10]:10} {res:9} "
                 f"{n['abates']}/{n['mortes']}/{n['assistencias']:<4} {farm:>6} {n['tempo_morto_s']:>5}s "
                 f"{n['ouro_parado_s']:>10.0f}s {n['objetivos_com_voce']:>4}/{n['objetivos_do_time']:<4}")
    L.append("")
    if agg.get("excluidas"):
        L.append("  * fora das médias (troll/afk no time)")
    L.append("Cada partida tem a análise completa em logs/<partida>/analise_partida.txt.")
    return "\n".join(L)


def run(use_sonnet: bool = True, log=print) -> Path | None:
    from .config import load_config
    cfg = load_config()
    log("Lendo as partidas em logs/ ...")
    games = load(log)
    if not games:
        log("Nenhuma partida com snapshots em logs/.")
        return None
    counted = [g for g in games if not g["fatos"]["numeros"].get("excluida")]
    agg = aggregate(counted or games)
    agg["resultados"] = dict(__import__("collections").Counter(g["fatos"]["numeros"]["resultado"] for g in games))
    agg["excluidas"] = [f"{g['fatos']['numeros']['pasta'][:16]} {g['fatos']['numeros']['campeao']} "
                        f"({g['fatos']['numeros'].get('motivo_exclusao', '')})" for g in games
                        if g["fatos"]["numeros"].get("excluida")]
    coach_text = None
    if use_sonnet and cfg["sonnet"].get("ativado", True):
        from .sonnet import Job, SonnetClient
        client = SonnetClient(cfg)
        if client.configured:
            job = Job("relatorio", 0, {
                "pedido": ("Relatório de várias partidas pro jogador, em português do Brasil. É texto pra ler. Com base só "
                           "nos números agregados e por partida, diga os 3 hábitos que mais custam partidas (cada um com o "
                           "número que mostra), o que melhorou (só se os números mostram) e até 3 tarefas concretas pras "
                           "próximas partidas. Com poucas partidas, diga que a amostra é pequena. Não invente nada."),
                "agregado": agg,
                "partidas": [g["fatos"]["numeros"] for g in counted],
                "prioridades_de_cada_partida": {g["fatos"]["numeros"]["pasta"]: g["prioridades"] for g in counted},
                "fora_da_analise": agg["excluidas"],
            }, SONNET_SCHEMA)
            try:
                a = client.ask(job)
                if a.data and not a.error:
                    coach_text = a.data
                    log(f"Sonnet: {a.latency:.1f}s, US$ {a.cost:.4f}")
            except Exception as e:
                log(f"Sonnet falhou ({e}); relatório só com os números")
    text = build_text(games, agg, coach_text)
    out = LOGS / "relatorio.txt"
    out.write_text(text, encoding="utf-8")
    (LOGS / "relatorio.json").write_text(json.dumps({"agregado": agg, "texto_sonnet": coach_text}, ensure_ascii=False,
                                                    indent=1), encoding="utf-8")
    log(text)
    log(f"\nSalvo em {out}")
    return out
