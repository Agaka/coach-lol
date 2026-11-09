"""Liga tudo: fonte de snapshots -> estado -> regras -> fila de voz -> logs."""
from __future__ import annotations

import json
import subprocess
import sys
import time
import traceback
from pathlib import Path

from .collector import LiveClient, replay
from .config import ROOT, load_champions, load_config
from .ddragon import DataDragon
from .logger import SessionLogger, _mmss
from .farm import FarmTracker
from .jungle import WHERE, JungleTracker
from .rotation import RotationCoach
from .rules import ALTA, NORMAL, Advice, RuleEngine
from .state import Change, StateTracker, lane_advantage
from .voice import SpeechQueue, Voice


BUILD_FILE = ROOT / "logs" / "build_atual.txt"


class Coach:
    def __init__(self, cfg: dict, champs: dict, dd: DataDragon, voice: Voice, replay_mode: bool = False,
                 advisor=None, jev_client=None, sonnet_client=None, elo=None):
        self.cfg, self.champs, self.dd, self.voice = cfg, champs, dd, voice
        self.sonnet_client = sonnet_client
        self.sonnet = None
        self.elo = elo
        self.replay_mode = replay_mode
        self.advisor = advisor
        self.jev_client = jev_client
        self.jev = None
        self.window = None
        self.active = False
        self.ended = False
        self.last_gt = 0.0
        self.state = None

    def start(self, raw: dict) -> None:
        self.tracker = StateTracker(self.dd, self.champs, self.cfg)
        self.rules = RuleEngine(self.cfg, self.dd)
        # Descobre o campeão pro nome da pasta antes da primeira atualização
        probe = StateTracker(self.dd, self.champs, self.cfg).update(raw)[0]
        cfg = self.cfg
        if self.replay_mode:
            cfg = {**cfg, "log": {**cfg["log"], "salvar_snapshots": False}}
        self.log = SessionLogger(cfg, probe.me.champ_id or "campeao", "_replay" if self.replay_mode else "")
        self.speech = SpeechQueue(self.voice, self.cfg["voz"]["intervalo_minimo_s"], self.log.advice)
        self.active, self.ended = True, False
        self.built: list[tuple[float, int]] = []
        self.last_build_t = -999.0
        self.last_build_item: int | None = None
        self.last_build_spoken = -999.0
        self.build_ignored: dict[int, int] = {}   # item recomendado -> vezes que você fechou outro no lugar
        self.farm = FarmTracker(self.cfg) if self.cfg["farm"].get("ativado", True) else None
        self.jungle = JungleTracker(self.cfg, self.dd) if self.cfg["cacador"].get("ativado", True) else None
        self.rotation = RotationCoach(self.cfg) if self.cfg["rotacao"].get("ativado", True) else None
        self.kill_spoken: dict[str, float] = {}   # alvo -> limite de vida da última fala de combo
        self.kill_spoken_t = -999.0
        self.kill_logged = ""
        self.timeline = []                 # linha do tempo pra análise pós-partida (coach/review.py)
        self.events: list[dict] = []
        self.purchases: list[str] = []
        self.purchase_times: list[float] = []
        self.review_data: dict | None = None
        if self.advisor:
            self.advisor.history.clear()
        if self.jev_client:
            from .jev import JevAdvisor, JevWorker
            jlog = self.log.dir / "jev.jsonl"

            def log_jev(rec: dict) -> None:
                with open(jlog, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")

            self.jev = JevAdvisor(self.cfg, JevWorker(self.jev_client, log_jev), self.speech, log_jev)
        self.pending_build = None          # (recomendação, tempo) esperando o Sonnet
        self.elo_notes: list[str] = []
        self.elo_logged = 0
        self.elo_prev_t: float | None = None
        if self.sonnet_client:
            from .sonnet import SonnetAdvisor, SonnetWorker
            slog = self.log.dir / "sonnet.jsonl"

            def log_sonnet(rec: dict) -> None:
                with open(slog, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                if rec.get("erro"):
                    self.log.text(f"[sonnet] erro ({rec['tipo']}): {rec['erro']}")

            # no replay as chamadas esperam a resposta, senão o tempo de jogo passa na frente
            self.sonnet = SonnetAdvisor(self.cfg, self.dd, SonnetWorker(self.sonnet_client, sync=self.replay_mode),
                                        self.speech, log_sonnet, self.elo, on_item=self._on_sonnet_item)
            self.sonnet.jungle = self.jungle
            self.sonnet.on_plan = self._on_plan
            self.sonnet.advisor_history = self.advisor.history if self.advisor else None
            self.sonnet.rotation = self.rotation
        opp = probe.opponent.champ_name if probe.opponent else "sem oponente definido"
        line = (f"== Partida: {probe.me.champ_name} ({probe.role}) vs {opp} | modo {probe.game_mode} "
                f"| patch Data Dragon {self.dd.version} | logs em {self.log.dir}")
        print(line)
        self.log.text(line)

    def feed(self, raw: dict) -> None:
        if not self.active:
            self.start(raw)
        self.log.snapshot(raw)
        state, changes = self.tracker.update(raw)
        self.state = state
        self.last_gt = state.game_time
        self._record(state, changes, raw)
        for c in changes:
            self._log_change(state, c)
        for a in self.rules.evaluate(state, changes):
            self.speech.push(a, state.game_time)
        if self.jungle:
            n = len(self.jungle.seen)
            players = state.allies + state.enemies
            for a in self.jungle.update(state, changes, lambda name: self.tracker.player_by_name(name, players)):
                self.speech.push(a, state.game_time)
            jg = self.jungle.find_jungler(state.enemies)
            if self.jungle.items_seen and self.jungle.items_seen[-1][0] == state.game_time:
                self.log.text(f"[{_mmss(state.game_time)}] item novo de {self.jungle.items_seen[-1][1]} apareceu "
                              f"(a API mostra atrasado: a compra pode ter sido uns 20 s antes)")
            for sg in self.jungle.seen[n:]:
                self.log.text(f"[{_mmss(state.game_time)}] jungler inimigo {jg.champ_name if jg else '?'} "
                              f"{WHERE[sg.side]} ({sg.how} às {_mmss(sg.t)}; nível {jg.level if jg else '?'}, farm {jg.cs if jg else '?'})")
        if self.rotation:
            players = state.allies + state.enemies
            for a in self.rotation.update(state, changes, lambda name: self.tracker.player_by_name(name, players),
                                          self.speech.last_by_key):
                self.speech.push(a, state.game_time)
        if self.farm:
            n = len(self.farm.curve)
            for a in self.farm.update(state, changes, None):
                self.speech.push(a, state.game_time)
            for mine, _, ocs in self.farm.curve[n:]:
                self.log.text(f"[{_mmss(mine.t)}] {self.farm.line(mine, opp_cs=ocs)}")
        if self.advisor:
            self._build_hooks(state, changes)
            if self.cfg["build"].get("aviso_combo", True):
                self._kill_hooks(state, changes)
        if self.jev:
            self.jev.tick(state, changes)
        if self.sonnet:
            self.sonnet.tick(state, changes, raw)
            if self.pending_build and state.game_time - self.pending_build[1] > float(self.cfg["sonnet"]["espera_item_s"]):
                rec = self.pending_build[0]
                self.pending_build = None
                self.log.text(f"[{_mmss(state.game_time)}] [build] Sonnet não respondeu a tempo; vale o cálculo")
                self._speak_build(rec, state, rec.ranking[0].item.id, rec.voice)
        if self.elo and self.elo.available:
            self._elo_hooks(state)
        self.speech.tick(state.game_time, state)
        if any(c.kind == "event" and c.data.get("EventName") == "GameEnd" for c in changes):
            result = next(c.data.get("Result") for c in changes if c.data.get("EventName") == "GameEnd")
            self.end(f"fim de jogo ({result})")

    def _record(self, s, changes: list[Change], raw: dict) -> None:
        from .review import Tick
        o = s.opponent
        st = s.stats or {}
        mana = (float(st.get("resourceValue", 0)) / max(1.0, float(st.get("resourceMax", 1)))
                if str(st.get("resourceType", "")).upper() == "MANA" else 1.0)
        self.timeline.append(Tick(s.game_time, s.hp_pct, s.gold, s.me.level, s.me.cs, s.me.is_dead, s.me.item_value,
                                  o.level if o else 0, o.cs if o else 0, o.item_value if o else 0, mana))
        self.events = (raw.get("events") or {}).get("Events", self.events)
        for c in changes:
            if c.kind == "purchase":
                self.purchase_times.append(s.game_time)
                self.purchases.append(f"{_mmss(s.game_time)} gastou ~{c.data['spent']:.0f}: "
                                      + ", ".join(self._iname(i) for i in c.data["new_items"]))

    def end(self, reason: str) -> None:
        if not self.active:
            return
        s = self.state
        self.speech.flush_report(self.last_gt)
        lines = [f"== Fim da sessão: {reason} aos {_mmss(self.last_gt)}"]
        if s:
            m = s.me
            lines.append(f"   {m.champ_name}: {m.kills}/{m.deaths}/{m.assists}, {m.cs} CS, nível {m.level}, "
                         f"itens: {', '.join(self._iname(i) for i in m.items if i) or '-'}")
            if s.opponent:
                o = s.opponent
                lines.append(f"   Oponente {o.champ_name}: {o.kills}/{o.deaths}/{o.assists}, {o.cs} CS, nível {o.level}")
        if self.jev:
            st = self.jev.stats
            lines.append(f"   Jev: {st['pedidos']} pedidos, {st['respostas']} respostas, {st['faladas']} viraram fala, "
                         f"{st['tokens']} tokens de entrada, US$ {st['custo']:.4f}" + (f" | desativado: {self.jev.worker.disabled_reason}"
                                                                if self.jev.worker.disabled_reason else ""))
        if self.sonnet:
            st = self.sonnet.stats
            lines.append(f"   Sonnet ({self.sonnet_client.provider}, {self.sonnet_client.model}): {st['pedidos']} pedidos, "
                         f"{st['respostas']} respostas, {st['faladas']} viraram fala, {st['erros']} erros, US$ {st['custo']:.4f}"
                         + (f" | desativado: {self.sonnet.worker.disabled_reason}" if self.sonnet.worker.disabled_reason else ""))
        if self.elo_notes:
            lines.append("ELO ALTO (" + self.elo.source() + ")")
            lines += [f"  {n}" for n in self.elo_notes]
        if self.rotation:
            rs = self.rotation.summary(s)
            lines.append(rs)
            (self.log.dir / "rotacao_resumo.txt").write_text(rs, encoding="utf-8")
        if self.farm:
            fs = self.farm.summary(s)
            if self.elo and self.elo.available and s and self.farm.role in ("TOP", "MIDDLE", "BOTTOM"):
                ref = self.elo.lane(s.role, s.me.champ_id, None).get("farm_aos_10")
                mine = next((m for m, _, _ in self.farm.curve if int(m.t // 60) == 10), None)
                if ref and mine:
                    fs += (f"\n  Elo alto ({s.me.champ_name} {s.role}): mediana {ref['mediana']} de farm aos 10 min, "
                           f"25% melhores {ref['p75']}+; você tinha {mine.cs}.")
            lines.append(fs)
            (self.log.dir / "farm_resumo.txt").write_text(fs, encoding="utf-8")
        review = ""
        if s and self.cfg["analise"].get("ativado", True) and len(self.timeline) > 60:
            try:
                from .review import build as build_review
                review = build_review(self, s)
                (self.log.dir / "analise_partida.txt").write_text(review, encoding="utf-8")
                if self.review_data:
                    (self.log.dir / "analise.json").write_text(
                        json.dumps(self.review_data, ensure_ascii=False, indent=1), encoding="utf-8")
                self.log.text(review)
                print(f"   Análise da partida: {self.log.dir / 'analise_partida.txt'}")
            except Exception:
                self.log.text("[análise] erro:\n" + traceback.format_exc())
        if self.advisor:
            summary = self.advisor.summary(self.built)
            lines.append(summary)
            (self.log.dir / "build_resumo.txt").write_text(summary, encoding="utf-8")
            last = self.advisor.history[-1].text if self.advisor.history else ""
            self._write_build_file((review + "\n\n" if review else "") + summary + "\n\n" + last)
        elif review:
            self._write_build_file(review)
        lines.append(f"   Logs: {self.log.dir}")
        for l in lines:
            print(l)
            self.log.text(l)
        if "fim de jogo" in reason:
            self.voice.say("Fim de jogo. Bom jogo!")
        self.log.close()
        self.active, self.ended = False, True

    # ---------------- build calculada ----------------
    def _build_hooks(self, s, changes: list[Change]) -> None:
        reason = None
        for c in changes:
            if c.kind == "item_completed" and c.player.key == s.me.key:
                self.built.append((s.game_time, c.data["item"]))
                rec_item = self.last_build_item
                if rec_item and c.data["item"] != rec_item and not self.dd.is_boots(c.data["item"]):
                    self.build_ignored[rec_item] = self.build_ignored.get(rec_item, 0) + 1
            elif c.kind == "my_death":
                reason = "morte"
            elif c.kind == "purchase" and reason is None and s.game_time - self.last_build_t > 45:
                reason = "base"
        if reason is None:
            return
        self.last_build_t = s.game_time
        try:
            rec = self.advisor.recommend(s, reason)
        except Exception:
            self.log.text("[build] erro no cálculo:\n" + traceback.format_exc())
            print("[build] erro no cálculo (detalhes no partida.log)")
            return
        if rec is None:
            return
        self.log.text(rec.text)
        with open(self.log.dir / "build.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(_rec_json(rec), ensure_ascii=False) + "\n")
        self._write_build_file(rec.text)
        built = [self._iname(i) for _, i in self.built]
        repeat = rec.ranking[0].item.id == self.last_build_item and s.game_time - self.last_build_spoken < 240
        # mesmo item de antes e sem fala prevista: não gasta chamada do Sonnet
        if self.sonnet and not repeat and self.sonnet.request_item(rec, s, built):
            self.pending_build = (rec, s.game_time)
            if self.sonnet.worker.sync:
                self.sonnet.tick(s, [], {})          # replay: a resposta já chegou
            return
        self._speak_build(rec, s, rec.ranking[0].item.id, rec.voice)

    def _speak_build(self, rec, s, item_id: int, text: str) -> None:
        # mesmo item de antes: só repete a fala depois de 4 minutos (a janela continua atualizando)
        if item_id == self.last_build_item and s.game_time - self.last_build_spoken < 240:
            return
        if self.build_ignored.get(item_id, 0) >= 2:
            # você já fechou outro item duas vezes no lugar desse: não insiste na voz (a janela continua)
            self.log.text(f"[{_mmss(s.game_time)}] [build] {self._iname(item_id)} não falado: ignorado 2x")
            return
        self.last_build_item, self.last_build_spoken = item_id, s.game_time
        c = next((c for c in rec.ranking if c.item.id == item_id), rec.ranking[0])
        self.speech.push(Advice(text, ALTA, "build", cooldown=40, ttl=30, created=s.game_time,
                                reason=f"build ({rec.reason}, {rec.chosen_by}): {c.per_1000:.2f}/1000g"), s.game_time)

    def _on_sonnet_item(self, rec, name, frase: str, motivo: str) -> str:
        """Resposta do Sonnet pro item. Os números da fala continuam vindo do cálculo."""
        s = self.state
        c = next((c for c in rec.ranking if c.item.name == name), None)
        if c is None:
            return "item fora dos candidatos"
        pos = rec.ranking.index(c) + 1
        rec.text += f"\n\nSONNET: {c.item.name} ({pos}º no cálculo, {c.per_1000:.2f}/1000g). {motivo}"
        self.log.text(f"[{_mmss(s.game_time)}] [build] Sonnet escolheu {c.item.name} ({pos}º no cálculo): {motivo}")
        self._write_build_file(rec.text)
        if not self.pending_build or self.pending_build[0] is not rec:
            return "atrasada (o cálculo já falou)"
        self.pending_build = None
        rec.chosen_id, rec.chosen_by = c.item.id, "Sonnet"
        if pos == 1:
            text = rec.voice                      # concorda: fala com os números do cálculo
        elif frase and len(frase.split()) <= 10:
            text = f"Próximo item: {c.item.name}. {frase}"
        else:
            text = f"Próximo item: {c.item.name}"
        self._speak_build(rec, s, c.item.id, text)
        return "enviada_pra_fala"

    def _on_plan(self, dica: str, frases: list[str], explicacao: str, kit: dict | None, opp: str | None) -> None:
        """Plano do matchup na janela (a mesma da build) pra ler no começo da partida."""
        L = [f"PLANO DO MATCHUP{f' contra {opp}' if opp else ''} (Sonnet, patch {self.dd.version})", ""]
        if dica:
            L.append(f"DICA: {dica}")
        L += [f"- {f}" for f in frases]
        L += ["", explicacao]
        if kit:
            L += ["", f"KIT DE {opp.upper() if opp else 'OPONENTE'} (Data Dragon)"]
            for k, v in kit.items():
                cd = f" | recarga {v['recarga_s']} s" if v.get("recarga_s") else ""
                L.append(f"{'P' if k == 'passiva' else k} {v['nome']}{cd}: {v['descricao']}")
        text = "\n".join(L)
        self.log.text(text)
        if not (self.advisor and self.advisor.history):   # não cobre uma build que já está na janela
            self._write_build_file(text)

    def _elo_hooks(self, s) -> None:
        """Aos 10 e 15 min, compara com as estatísticas de elo alto (log e contexto pro Sonnet)."""
        prev = s.game_time if self.elo_prev_t is None else self.elo_prev_t
        self.elo_prev_t = s.game_time
        if prev < 600 <= s.game_time and s.opponent:
            diff = s.me.item_value - s.opponent.item_value
            note = self.elo.lane_at(s.role, s.me.champ_id, s.opponent.champ_id, diff)
            if note:
                self.elo_notes.append(f"10:00 você {diff:+d} em itens contra {s.opponent.champ_name}: {note}")
        if prev < 900 <= s.game_time:
            diff = sum(p.item_value for p in s.allies) - sum(p.item_value for p in s.enemies)
            note = self.elo.team_at(diff)
            if note:
                self.elo_notes.append(f"15:00 seu time {diff:+d} em itens: {note}")
        for n in self.elo_notes[self.elo_logged:]:
            self.log.text(f"[{_mmss(s.game_time)}] [elo alto] {n}")
        self.elo_logged = len(self.elo_notes)

    def _kill_hooks(self, s, changes: list[Change]) -> None:
        """'Combo mata X abaixo de N% de vida' quando o seu poder ou o do alvo muda."""
        opp = s.opponent.key if s.opponent else None
        relevant = any(
            (c.kind in ("level_up", "item_completed") and c.player is not None and c.player.key in (s.me.key, opp))
            or c.kind == "purchase" for c in changes)
        if not relevant or s.me.is_dead or not s.enemies:
            return
        try:
            lines = self.advisor.kill_lines(s)
        except Exception:
            self.log.text("[combo] erro no cálculo:\n" + traceback.format_exc())
            return
        if not lines:
            return
        names = {e.key: e for e in s.enemies}
        text = "combo mata (vida cheia = 100%): " + ", ".join(
            f"{names[k].champ_name} ≤{thr:.0%}" + (f" (sobra {ratio - 1:.0%})" if ratio >= 1 else "")
            for k, (thr, ratio) in sorted(lines.items(), key=lambda x: -x[1][0]))
        if text != self.kill_logged:
            self.kill_logged = text
            self.log.text(f"[{_mmss(s.game_time)}] {text}")
        if s.game_time - self.kill_spoken_t < 90:
            return
        # rota: só o oponente; selva: o alvo mais fácil de gank que mudou desde a última fala
        if s.role == "JUNGLE" or opp is None:
            order = sorted((k for k in lines if not names[k].is_dead), key=lambda k: -lines[k][0])
            prefix = "Gank: " if s.role == "JUNGLE" else ""
        else:
            order, prefix = [opp], ""
        minimo = float(self.cfg["build"].get("aviso_combo_minimo", 0.3))
        key = next((k for k in order if lines[k][0] >= minimo
                    and abs(lines[k][0] - self.kill_spoken.get(k, -1.0)) >= 0.15), None)
        if key is None:
            return
        thr = lines[key][0]
        self.kill_spoken[key], self.kill_spoken_t = thr, s.game_time
        name = names[key].champ_name
        txt = (f"{prefix}Combo mata {name} com vida cheia" if thr >= 1
               else f"{prefix}Combo mata {name} abaixo de {int(5 * round(thr * 20))}% de vida")
        self.speech.push(Advice(txt, NORMAL, "combo_kill", cooldown=60, ttl=20, created=s.game_time,
                                reason=f"combo calculado contra {name} (todas as habilidades acertando, sem Incendiar)",
                                check=lambda st: not st.me.is_dead), s.game_time)

    def _write_build_file(self, text: str) -> None:
        BUILD_FILE.parent.mkdir(parents=True, exist_ok=True)
        BUILD_FILE.write_text(text, encoding="utf-8")
        if self.cfg["build"].get("janela", True) and (self.window is None or self.window.poll() is not None):
            exe = Path(sys.executable)
            pyw = exe.with_name("pythonw.exe")
            self.window = subprocess.Popen([str(pyw if pyw.exists() else exe), "-m", "coach.build.window",
                                            str(BUILD_FILE)], cwd=str(ROOT))

    def _iname(self, iid: int) -> str:
        it = self.dd.item(iid)
        return it["name"] if it else str(iid)

    def _log_change(self, s, c: Change) -> None:
        t = _mmss(s.game_time)
        me, opp = s.me, s.opponent
        msg = None
        if c.kind == "event":
            ev = c.data
            n = ev.get("EventName")
            if n == "ChampionKill":
                v = ev.get("victim")
                k = ev.get("killer")
                msg = f"abate: {k.champ_name if k else ev.get('KillerName')} matou {v.champ_name if v else ev.get('VictimName')}"
            elif n in ("DragonKill", "BaronKill", "HeraldKill", "HordeKill", "TurretKilled", "InhibKilled", "Ace",
                       "GameStart", "GameEnd", "FirstBlood"):
                extra = {k: v for k, v in ev.items() if k not in ("killer", "victim", "EventID", "EventTime", "EventName")}
                msg = f"evento {n} {extra}"
        elif c.kind == "purchase":
            msg = f"compra/base: +{', '.join(self._iname(i) for i in c.data['new_items'])} (gastou ~{c.data['spent']:.0f})"
        elif c.kind == "level_up" and (c.player.key == me.key or (opp and c.player.key == opp.key)):
            who = "você" if c.player.key == me.key else f"oponente {c.player.champ_name}"
            msg = f"nível {c.data['level']}: {who}"
        elif c.kind == "item_completed":
            msg = f"item completo: {c.player.champ_name} -> {self._iname(c.data['item'])}"
        elif c.kind == "my_death":
            k = c.data.get("killer")
            adv, why = lane_advantage(s)
            msg = f"VOCÊ MORREU (por {k.champ_name if k else '?'}), respawn {c.data['respawn']:.0f}s, vantagem {adv} {why}"
        elif c.kind == "my_respawn":
            msg = "você renasceu"
        if msg:
            self.log.text(f"[{t}] {msg}")


def _rec_json(rec) -> dict:
    return {
        "tempo_jogo": round(rec.game_time, 1), "motivo": rec.reason, "ouro": round(rec.gold),
        "voz": rec.voice, "alvo_principal": rec.main_target, "pesos": rec.weights, "perfil_dano": rec.profile,
        "comprar_agora": rec.buy_now,
        "ranking": [{
            "item": c.item.name, "id": c.item.id, "custo": c.cost, "falta": c.remaining,
            "dano_antes": {k: round(v, 3) for k, v in c.dmg_before.items()},
            "dano_depois": {k: round(v, 3) for k, v in c.dmg_after.items()},
            "ganho_dano_pp": round(c.dmg_gain, 2), "vida_efetiva": [round(c.ehp_before), round(c.ehp_after)],
            "ganho_vida_efetiva_pct": round(c.ehp_gain, 2), "anticura_pp": round(c.antiheal, 2),
            "pontos_por_1000": round(c.per_1000, 3), "motivo": c.main_reason, "obs": c.notes,
        } for c in rec.ranking],
    }


def _setup(no_voice: bool = False, no_jev: bool = False, no_sonnet: bool = False):
    cfg = load_config()
    champs = load_champions()
    dd = DataDragon().load()
    voice = Voice(cfg, enabled=not no_voice)
    advisor = None
    if cfg["build"].get("ativado", True):
        try:
            from .build.engine import BuildAdvisor
            advisor = BuildAdvisor(dd, champs, cfg)
        except Exception as e:
            print(f"[build] desativado: não consegui carregar os dados do CommunityDragon ({e})")
    jev_client = None
    if cfg["jev"].get("ativado", True) and not no_jev:
        from .jev import JevClient
        jev_client = JevClient(cfg)
        if jev_client.configured:
            print(f"[jev] ligado via {jev_client.provider} ({jev_client.model}, "
                  f"limiar {cfg['jev']['limiar_confianca']:.0%})")
        else:
            print("[jev] sem chave (OPENROUTER_API_KEY ou TYPESAFE_API_KEY): só as regras vão falar")
            jev_client = None
    sonnet_client = None
    if cfg["sonnet"].get("ativado", True) and not no_sonnet:
        from .sonnet import SonnetClient
        sonnet_client = SonnetClient(cfg)
        if sonnet_client.configured:
            print(f"[sonnet] ligado via {sonnet_client.provider} ({sonnet_client.model}, esforço {sonnet_client.effort})")
        else:
            print("[sonnet] sem chave (ANTHROPIC_API_KEY ou OPENROUTER_API_KEY): sem plano de matchup nem macro")
            sonnet_client = None
    from .elo import EloStats
    elo = EloStats(cfg)
    print(f"[elo alto] {elo.source()}" if elo.available else "[elo alto] sem estatísticas baixadas (baixar_estatisticas.bat)")
    if advisor is not None:
        advisor.elo = elo         # classe de item pra campeão fora da pool vem do que ele faz em elo alto
    return cfg, champs, dd, voice, advisor, jev_client, sonnet_client, elo


def _code_stamp() -> float:
    """Hora da última mudança no código e nas configs do coach."""
    files = list((ROOT / "coach").rglob("*.py")) + [ROOT / "config.yaml", ROOT / "champions.yaml"]
    return max((f.stat().st_mtime for f in files if f.exists()), default=0.0)


def _restart(reason: str) -> None:
    """Reinicia o próprio processo (mesmos argumentos): carrega o código novo sem fechar a janela."""
    import os
    print(f"\n[coach] {reason}: reiniciando pra carregar a versão nova...\n", flush=True)
    os.execv(sys.executable, [sys.executable, "-m", "coach"] + sys.argv[1:])


def run_live(no_voice: bool = False, no_jev: bool = False, no_sonnet: bool = False) -> None:
    started_code = _code_stamp()
    last_check = time.time()
    cfg, champs, dd, voice, advisor, jev_client, sonnet_client, elo = _setup(no_voice, no_jev, no_sonnet)
    client = LiveClient(cfg)
    coach = Coach(cfg, champs, dd, voice, advisor=advisor, jev_client=jev_client, sonnet_client=sonnet_client, elo=elo)
    interval = float(cfg["api"]["intervalo_polling_s"])
    draft = None
    if cfg["cliente"].get("draft", True):
        from .draft import DraftAdvisor
        draft = DraftAdvisor(cfg, dd, champs, elo, sonnet_client, voice if not no_voice else None,
                             coach._write_build_file)
        print("[draft] ligado: lê a seleção de campeões do cliente (só leitura)")
    print(f"Coach pronto (Data Dragon {dd.version}). Esperando partida... (Ctrl+C pra sair)")
    misses = 0
    finished_game = False   # depois do GameEnd, ignora a API até ela sumir
    try:
        while True:
            t0 = time.time()
            data = client.fetch()
            if data is None:
                misses += 1
                if coach.active and misses >= 10:
                    coach.end("a API parou de responder")
                if misses >= 3:
                    finished_game = False
                if draft is not None and not coach.active:
                    try:
                        draft.tick()          # fora da partida: lê o draft no cliente (só GET)
                    except Exception as e:
                        print(f"[draft] erro: {e}")
                # código atualizado? reinicia sozinho, mas só fora de partida e fora da seleção de campeões
                in_select = draft is not None and (draft.game_id is not None or draft.ready_seen is not None)
                if not coach.active and not in_select and time.time() - last_check > 10:
                    last_check = time.time()
                    if _code_stamp() > started_code + 1:
                        voice.wait_idle(5)
                        _restart("código do coach mudou")
                time.sleep(interval if coach.active else 2.0)
                continue
            misses = 0
            if finished_game:
                time.sleep(2.0)
                continue
            gt = float(data["gameData"].get("gameTime", 0))
            if coach.active and gt < coach.last_gt - 30:
                coach.end("nova partida detectada")
            coach.feed(data)
            if coach.ended:
                finished_game = True
                coach.ended = False
                print("Esperando a próxima partida...")
            time.sleep(max(0.0, interval - (time.time() - t0)))
    except KeyboardInterrupt:
        print("\nSaindo...")
        if coach.active:
            coach.end("encerrado pelo usuário")
    finally:
        voice.wait_idle(8)


def run_replay(path: Path, speed: float, no_voice: bool = False, no_jev: bool = False, no_sonnet: bool = False) -> None:
    cfg, champs, dd, voice, advisor, jev_client, sonnet_client, elo = _setup(no_voice, no_jev, no_sonnet)
    coach = Coach(cfg, champs, dd, voice, replay_mode=True, advisor=advisor, jev_client=jev_client,
                  sonnet_client=sonnet_client, elo=elo)
    print(f"Replay de {path} a {speed}x")
    try:
        for data in replay(path, speed):
            coach.feed(data)
            if coach.ended:
                break
    except KeyboardInterrupt:
        print("\nReplay interrompido")
    if coach.active:
        coach.end("fim do replay")
    voice.wait_idle(20)
