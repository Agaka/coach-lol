"""Carrega config.yaml e champions.yaml, com padrões quando faltar algo."""
from __future__ import annotations

import copy
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "api": {
        "url": "https://127.0.0.1:2999/liveclientdata",
        "intervalo_polling_s": 1.0,
        "certificado": "riotgames.pem",
    },
    "jogador": {"rota": "auto"},
    "voz": {
        "ativada": True,
        "voz": "pt-BR-AntonioNeural",
        "velocidade": "+15%",
        "volume": "+0%",
        "intervalo_minimo_s": 15,
    },
    "regras": {
        "ouro_guardado": 1500,
        "vida_baixa_pct": 0.30,
        "mana_baixa_pct": 0.2, "vida_critica_pct": 0.15,
        "vantagem_pressionar": 1.5,
        "vantagem_seguro": -1.5,
        "intervalo_avaliacao_lane_s": 75,
        "recompensa_abates": 3, "recompensa_ouro": 2500, "recompensa_intervalo_s": 240,
    },
    "objetivos": {
        "dragao_primeiro_s": 300,
        "dragao_respawn_s": 300,
        "anciao_respawn_s": 360,
        "vastilarvas_s": 480,
        "arauto_s": 900,
        "barao_s": 1200,
        "barao_respawn_s": 360,
        "avisos_s": [60, 30],
        "dragoes_para_alma": 4,
        "anciao_minimo_s": 0,
    },
    "objetivos_por_modo": {},
    "jev": {"ativado": True, "provedor": "auto", "modelo": "auto",
            "limiar_confianca": 0.70, "intervalo_s": 20, "atraso_max_s": 6, "timeout_s": 4},
    "build": {"ativado": True, "janela": True, "top": 5, "curadores": [],
              "aviso_combo": True, "aviso_combo_minimo": 0.30, "peso_meta": 0.3},
    "farm": {
        "ativado": True, "primeira_wave_s": 30, "intervalo_wave_s": 30, "tropas_por_wave": 6,
        "canhao": [[0, 3], [900, 2], [1500, 1]],
        "chegada_s": {"TOP": 38, "MIDDLE": 27, "BOTTOM": 38},
        "tempo_pra_matar_s": 25, "recall_s": 8,
        "volta_lane_s": {"TOP": 40, "MIDDLE": 28, "BOTTOM": 40},
        "falar_em_min": [5, 10, 14], "falar_se_perdeu": 8,
    },
    "farm_por_modo": {},
    "cliente": {"draft": True, "aceitar_partida": True, "aceitar_atraso_s": 1, "pasta": r"C:\Riot Games\League of Legends", "intervalo_s": 2},
    "analise": {"ativado": True, "sonnet": True},
    "rotacao": {"ativado": True, "wave_lateral": True, "wave_lateral_intervalo_s": 150},
    "cacador": {"ativado": True, "falar": True, "intervalo_s": 40, "sumido_s": 75, "ate_min": 20, "base_a_partir_s": 75},
    "sonnet": {
        "ativado": True, "provedor": "auto", "modelo": "auto", "esforco": "low", "timeout_s": 30,
        "plano_matchup": True, "item": True, "macro": True, "macro_intervalo_s": 210,
        "espera_item_s": 12, "item_minimo_do_melhor": 0.75, "atraso_max_macro_s": 20, "morto_minimo_s": 15, "preco_por_milhao": [2.0, 10.0],
    },
    "estatisticas": {
        "plataforma": "br1", "regiao": "americas", "elos": ["challenger", "grandmaster", "master"],
        "partidas_max": 1000, "partidas_por_jogador": 20, "amostra_minima": 20,
        "faixas_ouro_lane": [-1500, -500, 500, 1500], "faixas_ouro_time": [-4000, -1500, 1500, 4000],
    },
    "log": {"pasta": "logs", "salvar_snapshots": True},
}


def _merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_env_file(path: Path | None = None) -> None:
    """Lê chaves.env (CHAVE=valor por linha) pro ambiente. Variáveis já definidas no Windows ganham."""
    path = path or ROOT / "chaves.env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and v and not os.environ.get(k):
            os.environ[k] = v


def load_config(path: Path | None = None) -> dict:
    load_env_file()
    path = path or ROOT / "config.yaml"
    data = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return _merge(DEFAULTS, data)


def load_champions(path: Path | None = None) -> dict:
    path = path or ROOT / "champions.yaml"
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    generic = data.get("_generico", {})
    # Cada campeão herda do genérico o que não definir.
    return {k: (v if k == "_generico" else _merge(generic, v or {})) for k, v in data.items()}


def champion_profile(champs: dict, champ_id: str | None) -> tuple[dict, bool]:
    """Retorna (perfil, está_na_pool). Busca sem diferenciar maiúsculas."""
    if champ_id:
        for k, v in champs.items():
            if k.lower() == champ_id.lower() and k != "_generico":
                return v, True
    return champs.get("_generico", DEFAULTS_PROFILE), False


DEFAULTS_PROFILE = {
    "rota": "auto",
    "estilo": "generico",
    "alvo": "generico",
    "niveis_fortes": [6],
    "allin_nivel": 6,
    "itens_spike": 1,
    "peso_dano": 0.8,
    "peso_defesa": 0.3,
    "dicas_vantagem": [],
    "dicas_desvantagem": [],
}
