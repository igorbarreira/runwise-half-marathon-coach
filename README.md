# RunWise · Half Marathon Coach

Daily Oura + Garmin → Cloudflare Worker → Netlify static app for race-week training.

- **Frontend**: `index.html` → deployed at <https://runwise-igor.netlify.app>
- **API**: `worker/` → Cloudflare Worker `runwise-api.*.workers.dev` with KV cache
- **Sync**: `sync/sync_runwise.py` → runs in GitHub Actions every 2h (`.github/workflows/sync.yml`)

See [DEPLOY.md](DEPLOY.md) for the full architecture and setup notes.

## Sync via GitHub Actions

Six secrets must be configured at the repo level:

| Secret | Source |
|---|---|
| `RUNWISE_API` | `https://runwise-api.<account>.workers.dev` |
| `RUNWISE_SYNC_TOKEN` | Same value as `wrangler secret put SYNC_TOKEN` on the Worker |
| `GARMIN_EMAIL` | Garmin Connect login |
| `GARMIN_PASSWORD` | Garmin Connect password |
| `OURA_TOKEN` | Personal access token from <https://cloud.ouraring.com> |
| `GARMIN_TOKENS_B64` | Base64 of cached `~/.garminconnect/` tar — skips MFA in CI |

Refresh `GARMIN_TOKENS_B64` locally:

```bash
tar -czf - -C ~ .garminconnect | base64 | pbcopy
gh secret set GARMIN_TOKENS_B64 -b "$(pbpaste)"
```

Trigger a run manually:

```bash
gh workflow run sync.yml
gh run watch
```
