# Coach LoL

Lê a **Live Client Data API** oficial (`https://127.0.0.1:2999`) e o **Data Dragon**, e fala
conselhos curtos em português. Não lê memória, não captura tela, não mexe em mouse/teclado.

## Como usar
- **Ao vivo:** dê dois cliques em `iniciar_coach.bat`. Ele espera calado e conecta sozinho quando a partida carrega.
  Quando o código ou as configs mudam, ele se reinicia sozinho entre partidas (nunca durante a partida
  nem na seleção de campeões).
- **Teste sem o jogo:** `testar_exemplo.bat` toca uma partida sintética com voz.
- **Replay de uma partida gravada:** `.venv\Scripts\python -m coach replay logs\<pasta>\snapshots.jsonl.gz -v 4`
- **Estatísticas de elo alto:** `baixar_estatisticas.bat` (precisa de `RIOT_API_KEY` no `chaves.env`).
- Opções: `--sem-voz` (só texto), `--sem-jev`, `--sem-sonnet`, `-v N` (velocidade do replay), `testar-voz`.

## Arquivos
| Arquivo | Pra quê |
|---|---|
| `config.yaml` | voz, intervalo entre falas, limiares (ouro, vida), tempos de objetivos, rota fixa |
| `champions.yaml` | pool (Zed, Mel, Warwick, Garen, Miss Fortune, Leona, Gragas, Malzahar) + modo genérico; a ordem dentro da rota é a ordem do plano B no draft |
| `logs/<data>_<campeão>/` | `snapshots.jsonl.gz` (todo snapshot), `conselhos.jsonl` (cada conselho e se foi falado), `partida.log` (resumo legível) |

## Módulos (`coach/`)
`collector.py` (API + certificado da Riot + replay) · `ddragon.py` · `state.py` (estado, mudanças, timers, vantagem) ·
`rules.py` (regras) · `voice.py` (edge-tts + fila com prioridade) · `logger.py` · `app.py` · `sample.py` (partida sintética)

## Jev — decisões rápidas (`coach/jev.py`, fase 2)
A cada ~20 s de jogo (ou antes, em abates, itens, níveis 2/3/6) o coach manda um estado resumido
(só campeões e números, sem nomes de jogador) pro Jev da TypeSafe (`POST https://api.typesafe.ai/v1/systemone`)
com uma pergunta `choice` de opções fixas: pressionar, jogar_seguro, farmar, voltar_base, agrupar_objetivo, ajudar_vizinho.
- Só fala se `confidence` ≥ `jev.limiar_confianca` (padrão 0,70), se a resposta não chegou atrasada
  e se nenhuma regra já disse a mesma coisa nos últimos 25 s.
- Chave por variável de ambiente, dois provedores com o mesmo formato:
  - OpenRouter: `OPENROUTER_API_KEY` + `OPENROUTER_JEV_MODEL` (ex.: `typesafe/jev-1.13`) → `https://openrouter.ai/api/v1/systemone`
  - TypeSafe direto: `TYPESAFE_API_KEY` (opcional `TYPESAFE_API_BASE`)
  - `jev.provedor: auto` usa o que tiver chave. Sem chave, roda só com regras.
- Tudo vai pra `logs/<partida>/jev.jsonl`: estado enviado, escolha, confiança, probabilidades, latência e o que virou fala.
- `--sem-jev` desliga numa execução.

## Rotação (`coach/rotation.py`)
Ideia central: empurre a wave antes de sair da rota. O coach diz pra onde ir e quando, pela sua função:
- 1 minuto antes de cada objetivo: "Dragão em 1 minuto: empurre o mid e vá pro rio de baixo" (mid),
  "faça os campos de baixo e vá pro dragão" (jungle), "empurrem a wave e entrem no rio" (bot)...
- Torre externa da sua rota caiu: pra onde rodar (ou "farme perto da interna", se foi a sua).
- Meio de jogo parado: "Pegue a wave do bot antes do dragão".
- Fim de jogo: `rotacao_resumo.txt` com os objetivos que o seu time fez **sem você, com você vivo**
  (o sinal mais claro de rotação atrasada), objetivos do inimigo e torres por rota.
- Torres vêm do nome do evento (`Turret_TChaos_L0_P3`: L0 bot, L1 mid, L2 topo; P3 externa, P2 interna, P1 inibidor).

## Draft (`coach/draft.py`)
Na seleção de campeões o coach lê a API local do cliente (LCU) — **só leitura**, nunca escolhe, bane ou
aceita nada (exceção à regra "só Live Client Data API" liberada só pra isso). Lê o lockfile da pasta do LoL
(`cliente.pasta`), sua rota, bans e picks, e fala/mostra na janela:
- sugestão de ban (counter do seu principal ou campeão forte no elo alto);
- pick da sua pool, mantendo o principal (o 1º da rota no champions.yaml) a menos que esteja banido ou o oponente
  seja counter claro; usa o seu histórico de vitórias por campeão (logs/), não a vitória geral de Mestre+.
  `draft: false` no champions.yaml tira um campeão das sugestões. Contra os inimigos escolhidos, o oponente de rota é deduzido olhando o time inimigo inteiro
  (cada um numa rota, pela frequência de cada campeão em cada rota no elo alto). Abaixo de 60% de chance ele
  diz "oponente ainda indefinido" ou "X ou Y no meio" e só fala de novo quando isso muda; se banirem o
  principal, o próximo da pool;
- depois de travar: página de runas e feitiços mais usados em Mestre+ (precisa das partidas completadas com
  runas: `baixar_estatisticas.bat` com chave nova da Riot).
Registro em `logs/draft.jsonl`. Desliga com `cliente.draft: false`.
- **Aceitar a partida:** a única ação que o coach faz no cliente (liberada pelo usuário). Quando aparece a tela
  de partida encontrada e você ainda não respondeu, ele aceita depois de `cliente.aceitar_atraso_s` e fala
  "Partida aceita". Se você recusar, ele não mexe. Não entra na fila sozinho. Desliga com
  `cliente.aceitar_partida: false`.

## Análise da partida (`coach/review.py`)
No fim de cada partida sai `analise_partida.txt` (também na janela): o que você fez certo e errado e até
3 prioridades pra próxima. Os fatos vêm da partida:
- cada morte com vida 10 s antes, ouro no bolso, nível e itens contra o oponente, quem participou e o que
  o coach tinha falado antes da luta, com diagnóstico (gank, ouro sem gastar, luta de vida cheia em desvantagem);
- abates e roaming, farm e nível aos 5/10/15 min contra o oponente e a mediana de Mestre+, farm parado;
- tempo sem mana na rota e quando chegou a primeira tropa;
- voltas pra base e ouro parado;
- objetivos, separando o que o time fez sem você, o que o inimigo fez com você vivo e o que foi **troca**
  (seu time fez abate/torre em outro lugar ao mesmo tempo).
O Sonnet (`analise.sonnet`) só escreve o texto em cima desses fatos (~US$ 0,01 por partida). A API não mostra
posição, ping nem chat: o que o time combinou não aparece.

## Relatório de várias partidas (`coach/report.py`)
`relatorio.bat` junta todas as partidas reais de `logs/` em `logs/relatorio.txt`: hábitos que se repetem,
tipos de morte somados, farm aos 10, ouro parado, objetivos, se você seguiu o coach (voltar / jogar seguro,
sem contar "vida crítica", que toca no meio da luta), por campeão, tendência e partida a partida.
Cada partida grava `analise.json`; as antigas são reprocessadas pelos snapshots, sem custo. Partida sem
resultado (o jogo fechou antes do fim): crie `resultado.txt` na pasta dela com `vitória` ou `derrota`.
Partida que não diz nada sobre como você joga (troll ou afk no seu time): `derrota (troll)` no `resultado.txt`;
ela fica na tabela, mas sai das médias e dos hábitos.
O Sonnet escreve resumo, 3 hábitos e plano em cima dos números (~US$ 0,02).

## Jungler inimigo (`coach/jungle.py`)
A API não dá posição; o coach deduz pelas pistas que ela entrega:
- abate, torre ou inibidor com o nome dele → o lado do mapa é a rota dos outros participantes;
- objetivo do time inimigo → dragão = rio de baixo; vastilarvas, Arauto e Barão = rio de cima;
- item novo **não** é usado pra dizer que ele foi pra base: a API mostra o item do inimigo atrasado
  (numa partida real, 18 a 22 s depois da compra, quando ele já voltava pra rota);
- nível 3 antes dos 4 min e nível 4 antes dos 5 min → terminou campos, pode gankar.
Falas pra quem está numa rota ("Jungler inimigo no bot: pode jogar pra frente", "... perto de você", pro mid "no rio de cima: cuidado no mid" quando foi objetivo,
"sumido faz tempo") e pra quem é jungler ("Jungler deles no topo: jogue no
lado de baixo").
Cada pista vai pro `partida.log` e pro macro do Sonnet. Ajustes em `cacador` no `config.yaml`.

## Sonnet — momentos sem pressa (`coach/sonnet.py`, fase 3)
SDK oficial da Anthropic, modelo `claude-sonnet-5-5`. Com `ANTHROPIC_API_KEY` vai direto na Anthropic (com fallback
de recusa no servidor); sem ela, usa a `OPENROUTER_API_KEY` (a mesma do Jev) com `anthropic/claude-sonnet-5.5`.
- **Começo da partida:** dica de matchup falada ("O E da Ahri, o beijo que encanta, tem 12 segundos: troque
  quando ela errar"). Habilidade sempre pela tecla + o que ela faz, nunca só o nome + até 2 frases (níveis fortes, all-in). A janela abre com o plano completo e o kit do oponente
  (passiva, habilidades e recargas do patch). O Sonnet recebe o kit de todo mundo e os feitiços reais.
- **Textos do patch:** descrições de itens e habilidades vêm do Data Dragon em português, da versão atual,
  em toda chamada; o Sonnet não depende do que lembra do treinamento.
- **Morte/base:** recebe o ranking do cálculo e escolhe entre os candidatos que têm pelo menos 75% da pontuação
  do 1º. Se concorda com o 1º, a fala é a do cálculo (com os números dele); se troca, fala o item e o porquê sem
  números, e a janela mostra a posição do item no cálculo. Se não responder em 12 s, vale o cálculo.
- **A cada 3,5 min:** reavaliação do macro em uma frase (estratégia de mapa; ouro, vida e timers ficam com as regras).
- Tudo em `logs/<partida>/sonnet.jsonl` (pedido, resposta, latência, tokens, custo). Custo medido: ~US$ 0,07–0,10 por partida.
- No replay as chamadas esperam a resposta (senão o tempo de jogo passa na frente). `--sem-sonnet` desliga.

## Elo alto (`coach/elo.py`, fase 4)
Baixa partidas ranqueadas solo/duo de Desafiante/Grão-Mestre/Mestre pela API web da Riot (league-v4 + match-v5
com timeline) e guarda um resumo de cada uma em `data/elo_alto/<plataforma>/partidas/`.
- Chave: `RIOT_API_KEY` no `chaves.env` (chave de desenvolvimento em developer.riotgames.com, expira em 24 h).
  Limite da chave: ~50 partidas a cada 2 min; o download continua de onde parou.
- Estatísticas: vitória por faixa de diferença de ouro em itens aos 10 min (matchup exato e campeão na rota),
  vitória do time por diferença aos 15 min, e farm aos 10 min (mediana e 25% melhores).
- Meta: campeões mais jogados e vitória por rota, e itens completos mais feitos por campeão (uso e vitória).
  Vai como contexto pro Sonnet; a build continua vindo do cálculo contra o time inimigo de verdade.
- Uso: contexto pro Sonnet (plano, item e macro), notas aos 10/15 min no `partida.log` e comparação de farm no resumo.
- Novo patch: rode `baixar_estatisticas.bat` de novo (chave nova se a antiga venceu) pra atualizar o meta.
  Amostra menor que `estatisticas.amostra_minima` (20) não vira estatística.

## Build calculada (`coach/build/`)
Quando você morre ou volta pra base, o coach simula **cada item lendário/bota que você pode fazer**
contra **cada inimigo** (campeão + nível + itens dele, vindos da Live API) e ranqueia por ganho por 1000 de ouro.
- Dados do patch: Data Dragon (preço, receita) + CommunityDragon (atributos estruturados, valores das passivas,
  fórmulas das habilidades). Cache em `data/cdragon/<patch>/`. Nada de número fixo no código.
- Combo da pool escrito à mão em `combos.py` (ordem das habilidades); dano de cada habilidade sai das fórmulas do patch.
- Mitigação 100/(100+resistência), redução → % pen → letalidade, Cutelo Negro, Lembranças do Lorde Dominik,
  Eclipse, Rei Destruído, Liandry, Hidra Titânica, Shojin, Coletora, Força da Trindade, escudos de Rookern/Malmortius/Eclipse, anticura.
- Vida efetiva contra o perfil físico x mágico somado dos itens inimigos.
- Pesos por campeão (`peso_dano`, `peso_defesa`, `alvo`) no `champions.yaml`; curadores pra anticura no `config.yaml`.
- Campeão fora da pool: a classe de item vem do que ele faz em Mestre+ (atributos dos itens mais feitos);
  sem amostra, da classe do Data Dragon (lutador sem letalidade/crítico, tanque, mago, atirador, assassino).
- Pontuação = ganho por 1000 de ouro que **ainda falta** gastar (com componentes na mão, terminar o item vale mais).
- O item recomendado da última vez sempre volta como candidato, e o Sonnet só troca por motivo forte novo.
- Meta de Mestre+ pesa no ranking: item feito por X% dos jogadores do campeão ganha `build.peso_meta`·X na
  pontuação (Cicloespada no Zed, 97%: +29%). Item começado (componentes valendo 40%+ do preço) fica até
  terminar. Item que você ignorou 2 vezes não é mais falado (continua na janela).
- Cicloespada Voltaica modelada (ataque energizado: % da vida atual + letalidade, valores do patch).
- Contra assassino (tag principal "Assassin" no Data Dragon), itens com estase (Ampulheta de Zhonya, Anjo
  Guardião) sempre vão pro Sonnet como candidatos: o ativo não entra no cálculo, mas anula o burst.
- Saída: voz curta ("Próximo item: X. Combo tira 94% de Ahri"), janela com a explicação completa
  (`logs/build_atual.txt`), `build.jsonl` com todos os números, e `build_resumo.txt` (feita x recomendada) no fim.

## "Combo mata X abaixo de N% de vida"
A Live API **não** informa a vida atual nem a posição dos inimigos. Então o coach calcula, com o mesmo combo
da build, até que % de vida o seu combo mata cada inimigo, e você compara com a barra de vida na tela.
- Recalcula quando você sobe de nível ou compra, e quando o oponente fecha item. Rota: fala do oponente;
  selva: "Gank: ..." com o alvo mais fácil. Só fala de novo se o limite mudar 15 pontos (mínimo 90 s entre falas).
- Assume todas as habilidades acertando e fora de recarga, sem Incendiar. Todos os limites ficam no `partida.log`.
- Ajuste em `build.aviso_combo` / `build.aviso_combo_minimo` no `config.yaml`.

## Tropas perdidas (`coach/farm.py`)
Só pra TOP/MIDDLE/BOTTOM. O máximo possível sai do calendário de waves do `config.yaml` (seção `farm`; o
horário da 1ª wave vem do evento `MinionsSpawning` do jogo). As perdas são divididas em **morto** (isDead),
**base** (recall + compra + caminho de volta, e o caminho depois de renascer) e **rota** (o resto: último golpe
errado, rotação, outra rota). O creepScore da API sobe de 10 em 10, então a margem é ±10.
- Voz nos minutos de `farm.falar_em_min` ("Perdeu umas 40 tropas, 30 na rota"), linha por minuto no `partida.log`
  e `farm_resumo.txt` no fim com a curva de 2 em 2 minutos e a comparação com o oponente.

## Limitações conhecidas
- Recompensa: a API não mostra; o coach conta seus abates seguidos (3, 5, 7: "Você vale recompensa: não ande
  sozinho") e, depois dos 18 min, itens 2500+ acima da média inimiga ("Muito na frente: lute junto com o time").
- "Nível N primeiro, troque agora" só nos níveis fortes do campeão (`niveis_fortes`); "Pouca mana" abaixo de
  `regras.mana_baixa_pct` na fase de rota.
- Itens dos inimigos chegam atrasados na API (~20 s): o coach não avisa "voltou pra base" de inimigo.
- Fala do Sonnet que conta com inimigo morto é descartada se ele renascer antes de ela ser falada; inimigo que
  renasce em menos de 15 s já vai pro Sonnet como vivo.
- A API não dá posição, vida atual nem cooldowns do inimigo: a vantagem assume o inimigo com vida cheia.
- Na Ferramenta de Treino/personalizada a posição vem vazia; se o oponente não for detectado, fixe `jogador.rota` no `config.yaml`.
- Os tempos de objetivo de 2026 estão no `config.yaml` (Barão 20:00 confirmado; os outros conferir no patch).
- Dicas por campeão que dependem de feitiço usam `requer_feitico` no `champions.yaml` (ex.: Incendiar = SummonerDot).
- Build: o combo é um único all-in ideal; aceleração de habilidade, velocidade de ataque e de movimento não pontuam.
  Passivas fora da lista acima aparecem como "não modelada" na explicação. Runas e feitiços (Incendiar) não entram.
