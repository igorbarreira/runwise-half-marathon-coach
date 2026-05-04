# RunWise · Deploy

App de meia maratona com dados ao vivo de Oura + Garmin.

```
runwise-half-marathon-coach/
├── index.html          # Frontend (deploy no Netlify)
├── worker/             # Cloudflare Worker (API)
│   ├── src/index.js
│   ├── wrangler.toml
│   └── package.json
├── sync/               # Sync local Mac → Worker (cron horário)
│   ├── sync_runwise.py
│   ├── requirements.txt
│   └── com.runwise.sync.plist
└── DEPLOY.md
```

## Arquitetura

```
┌──────────────┐     POST /api/sync      ┌──────────────────┐
│ Mac (cron 1h)│ ──────────────────────► │  Cloudflare      │
│ sync_runwise │   Bearer SYNC_TOKEN     │  Worker + KV     │
└──────────────┘                         └─────┬────────────┘
                                               │
                            GET /api/snapshot  │  GET Oura API
                                               │  (Bearer)
                                               ▼
                                       ┌──────────────────┐
                                       │ Netlify (HTML)   │
                                       │ runwise-igor     │
                                       └──────────────────┘
```

- **Oura** é puxado direto pelo Worker em cada `GET /api/snapshot` (cache 60s) — não precisa de sync local.
- **Garmin** não tem API pública decente, então um cron local em Python loga via `garminconnect` e empurra um JSON compacto para o Worker (`POST /api/sync`).
- O frontend só faz `GET /api/snapshot` e usa um **snapshot embutido** como fallback se o Worker estiver inacessível.

---

## 1. Cloudflare Worker

Precisa de uma conta Cloudflare (free) e o `wrangler` CLI.

```bash
cd worker
npm install -g wrangler              # ou npx wrangler ...
wrangler login

# Cria o KV namespace e copia o id
wrangler kv namespace create RUNWISE
# saída: id = "abc123..."  ← cole esse id em wrangler.toml

# Define os segredos
wrangler secret put OURA_TOKEN       # cole o Personal Access Token de cloud.ouraring.com
wrangler secret put SYNC_TOKEN       # gere um secret aleatório:  openssl rand -hex 32

wrangler deploy
# saída: https://runwise-api.<account>.workers.dev
```

Teste:
```bash
curl https://runwise-api.<account>.workers.dev/api/health
curl https://runwise-api.<account>.workers.dev/api/snapshot | jq
```

## 2. Frontend (Netlify)

Edite `index.html` e troque a constante:

```js
const API_BASE = 'https://runwise-api.<account>.workers.dev';
```

Faça o deploy normal (drag-and-drop, Netlify CLI, ou Git). O app já está hospedado em `runwise-igor.netlify.app`.

Sem nenhuma config: continua funcionando com o snapshot embutido (datado de 2026-05-02).

## 3. Sync local (Mac, cron horário)

```bash
cd sync
pip3 install -r requirements.txt

# Teste manual (com env vars):
RUNWISE_API="https://runwise-api.<account>.workers.dev" \
RUNWISE_SYNC_TOKEN="<o mesmo SYNC_TOKEN>" \
GARMIN_EMAIL="seu@email" \
GARMIN_PASSWORD="senha" \
OURA_TOKEN="<opcional, para pré-cachear>" \
python3 sync_runwise.py
```

Para rodar de hora em hora via launchd:

1. Edite `com.runwise.sync.plist` (substituir as 5 strings em `EnvironmentVariables`).
2. Copie e carregue:
   ```bash
   cp com.runwise.sync.plist ~/Library/LaunchAgents/
   launchctl load -w ~/Library/LaunchAgents/com.runwise.sync.plist
   ```
3. Logs ficam em `/tmp/runwise-sync.log` e `/tmp/runwise-sync.err`.

Para parar:
```bash
launchctl unload -w ~/Library/LaunchAgents/com.runwise.sync.plist
```

## Custo

- Cloudflare Workers free tier: 100k requests/dia, 10ms CPU/req — sobra muito.
- Cloudflare KV free: 100k reads/dia, 1k writes/dia — sync horário usa ~24 writes/dia.
- Netlify free.
- **Total: $0/mês.**

## Troubleshooting

| Sintoma | Causa provável | Fix |
|---|---|---|
| Badge "Offline" no app | Worker fora ou CORS bloqueado | `curl` o `/api/health`; checar console do browser |
| Badge "Snapshot baked-in" | `API_BASE` ainda com `YOUR-SUBDOMAIN` | Edite `index.html` linha do `API_BASE` |
| `wrangler` reclama do KV id | `wrangler.toml` ainda com placeholder | Cole o id retornado por `wrangler kv namespace create` |
| Sync local falha com 401 | `SYNC_TOKEN` desalinhado | `wrangler secret put SYNC_TOKEN` igual ao do plist/env |
| Sync falha com auth Garmin | Garmin pediu MFA | Logar no Garmin Connect web e completar MFA — token persiste por semanas |
| Última corrida não atualiza | Sync não rodou | `tail /tmp/runwise-sync.log` |

## Push de treinos para o relógio

A v1 tinha botões "Enviar para Garmin" — removidos na v2 porque exigiriam que a auth Garmin estivesse no Worker (complexo). Por enquanto, use a skill MCP local do Garmin no Claude Code:

```
"sobe os treinos do RunWise da semana pro Garmin"
```

A próxima iteração pode adicionar isso ao `sync_runwise.py` (chamada `g.upload_workout(payload)` + `g.schedule_workout(...)`).

## v2 — o que mudou

- ✅ Semáforo de treinabilidade (verde/amarelo/vermelho) com lógica adaptativa por tipo de sessão
- ✅ Recap completo da última corrida com sparkline de splits (pace + FC)
- ✅ Carga semanal: barra km feito vs planejado, ACWR, tempo em zonas
- ✅ Plano marca dias `done` quando há atividade no Garmin
- ✅ Coach briefing inline adaptado ao semáforo
- ✅ Backend Cloudflare Worker (free) com sync local hourly
- ✅ Snapshot embutido como fallback offline
- ✅ Layout mais limpo (zonas + método agora colapsáveis)
