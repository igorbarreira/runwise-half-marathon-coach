#!/usr/bin/env python3
"""RunWise local sync.

Pulls a compact daily snapshot from Garmin Connect (last 7 days of running
activities + today's training status, readiness and HR summary) and pushes the
JSON to the RunWise Cloudflare Worker so the public app can render fresh data
without ever holding Garmin credentials in the browser.

Usage:
    export RUNWISE_API="https://runwise-api.<account>.workers.dev"
    export RUNWISE_SYNC_TOKEN="<same value set as Worker secret>"
    export GARMIN_EMAIL="..."
    export GARMIN_PASSWORD="..."
    # optional, also pushes Oura readiness if you want a pre-cached snapshot
    export OURA_TOKEN="..."

    python3 sync_runwise.py

Tested with garminconnect>=0.2.20.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

try:
    from garminconnect import Garmin
    import garth
except ImportError:
    print("missing dep: pip install garminconnect", file=sys.stderr)
    sys.exit(2)


def env(name: str, required: bool = True) -> str | None:
    v = os.environ.get(name)
    if required and not v:
        print(f"missing env: {name}", file=sys.stderr)
        sys.exit(2)
    return v


def safe(fn, *args, **kwargs):
    """Call a Garmin endpoint, swallow errors, return None on failure."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001
        print(f"warn: {fn.__name__} failed: {e}", file=sys.stderr)
        return None


def shrink_activity(a: dict) -> dict:
    """Pick only the fields the app actually consumes."""
    keys = (
        "activityId",
        "activityName",
        "startTimeLocal",
        "distance",
        "duration",
        "averageHR",
        "maxHR",
        "averageSpeed",
        "calories",
        "steps",
        "trainingEffectLabel",
        "aerobicTrainingEffect",
        "anaerobicTrainingEffect",
        "averageRunningCadenceInStepsPerMinute",
        "activityType",
    )
    out = {}
    for k in keys:
        if k in a:
            out[k] = a[k]
    # Normalize activity type to a string
    t = a.get("activityType") or {}
    if isinstance(t, dict):
        out["activityType"] = t.get("typeKey", "running")
    return out


def shrink_splits(splits_raw: dict | None) -> list[dict]:
    if not splits_raw:
        return []
    laps = splits_raw.get("lapDTOs") or splits_raw.get("laps") or []
    out = []
    for lap in laps:
        # lapDTOs (full payload) vs laps (compact MCP shape) — handle both.
        out.append({
            "n": lap.get("lapIndex") or lap.get("lap_number"),
            "dist": lap.get("distance") or lap.get("distance_meters"),
            "dur": lap.get("duration") or lap.get("duration_seconds"),
            "hr": lap.get("averageHR") or lap.get("avg_hr_bpm"),
            "max_hr": lap.get("maxHR") or lap.get("max_hr_bpm"),
            "spd": lap.get("averageSpeed") or lap.get("avg_speed_mps"),
            "cad": lap.get("averageRunCadence") or lap.get("avg_cadence"),
        })
    return out


GARMIN_TOKEN_DIR = os.path.expanduser("~/.garminconnect")


def hydrate_garmin_tokens_from_env() -> None:
    """When running in CI (GitHub Actions), restore ~/.garminconnect from a
    base64-encoded tar.gz passed via GARMIN_TOKENS_B64. The runner's home dir
    starts empty, so without this we'd be forced through MFA every run.

    Encode locally with:
        tar -czf - -C ~ .garminconnect | base64 | pbcopy
    """
    b64 = os.environ.get("GARMIN_TOKENS_B64")
    if not b64:
        return
    import base64
    import io
    import tarfile
    raw = base64.b64decode(b64)
    parent = os.path.dirname(GARMIN_TOKEN_DIR)
    os.makedirs(parent, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        tar.extractall(parent)
    print(f"hydrated garmin tokens from env into {GARMIN_TOKEN_DIR}", file=sys.stderr)


def _mfa_prompt() -> str:
    if not sys.stdin.isatty():
        raise RuntimeError(
            "Garmin MFA required but stdin is not a TTY (likely CI). "
            "Re-run sync locally on your Mac to refresh GARMIN_TOKENS_B64 secret."
        )
    return input("Garmin MFA code: ")


def _underlying_client(g: Garmin):
    """Return the underlying garth client from a Garmin instance.

    Different garminconnect versions expose it as `.garth` or `.client`,
    and some don't expose it at all (in which case fall back to the global
    `garth.client`).
    """
    return getattr(g, "garth", None) or getattr(g, "client", None) or garth.client


def garmin_client() -> Garmin:
    """Return an authenticated Garmin client.

    Strategy:
      1. Resume garth session from cached tokens.
      2. If that fails, do a fresh garth login (interactive MFA if Garmin asks).
      3. Persist tokens to disk so subsequent runs are non-interactive.
      4. Return a `garminconnect.Garmin` instance bound to the authenticated
         garth client.
    """
    # 0. If running in CI, hydrate the token dir from a base64 secret.
    hydrate_garmin_tokens_from_env()

    # 1. Try cached tokens via garth directly (more reliable than Garmin.garth.load)
    try:
        garth.resume(GARMIN_TOKEN_DIR)
        # Validate: cheap call that 401s if token expired
        garth.client.username  # noqa: B018
        g = Garmin()
        # Inject the authenticated garth session into the Garmin client
        if hasattr(g, "garth"):
            g.garth = garth.client
        elif hasattr(g, "client"):
            g.client = garth.client
        # Final sanity check via a Garmin API call
        g.get_full_name()
        return g
    except Exception as e:
        print(f"cached token unusable ({e}); doing fresh login…", file=sys.stderr)

    # 2. Fresh login via garth (handles MFA via prompt)
    email = env("GARMIN_EMAIL")
    password = env("GARMIN_PASSWORD")
    try:
        garth.login(email, password, prompt_mfa=_mfa_prompt)
    except TypeError:
        # Older garth doesn't accept prompt_mfa kwarg
        garth.login(email, password)

    os.makedirs(GARMIN_TOKEN_DIR, exist_ok=True)
    garth.save(GARMIN_TOKEN_DIR)
    print(f"saved garmin tokens to {GARMIN_TOKEN_DIR}", file=sys.stderr)

    g = Garmin()
    if hasattr(g, "garth"):
        g.garth = garth.client
    elif hasattr(g, "client"):
        g.client = garth.client
    return g


def collect_garmin() -> dict:
    g = garmin_client()

    today = date.today()
    start = today - timedelta(days=10)

    activities_raw = safe(g.get_activities_by_date, start.isoformat(), today.isoformat(), "running") or []
    activities = [shrink_activity(a) for a in activities_raw]

    last_activity_splits = []
    if activities_raw:
        last_id = activities_raw[0].get("activityId")
        if last_id:
            last_activity_splits = shrink_splits(safe(g.get_activity_splits, last_id))

    readiness = safe(g.get_training_readiness, today.isoformat())
    status = safe(g.get_training_status, today.isoformat())
    predictions = safe(g.get_race_predictions)
    hr_summary = safe(g.get_heart_rates, today.isoformat())

    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "activities": activities,
        "last_activity_splits": last_activity_splits,
        "training_readiness": readiness,
        "training_status": status,
        "race_predictions": predictions,
        "hr_summary": hr_summary,
    }


def collect_oura() -> dict | None:
    token = env("OURA_TOKEN", required=False)
    if not token:
        return None
    today = date.today()
    start = today - timedelta(days=7)

    def _get(path: str) -> dict:
        url = f"https://api.ouraring.com/v2/usercollection/{path}?start_date={start}&end_date={today}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "RunWise-Sync/1.0",
        })
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))

    try:
        return {
            "readiness": _get("daily_readiness").get("data", []),
            "sleep": _get("daily_sleep").get("data", []),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        print(f"warn: Oura fetch failed: {e}", file=sys.stderr)
        return None


def push(api_base: str, token: str, payload: dict) -> None:
    url = api_base.rstrip("/") + "/api/sync"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "RunWise-Sync/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print(f"sync ok: {r.status} {r.read().decode('utf-8')}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"sync FAILED: HTTP {e.code} — {body}", file=sys.stderr)
        raise


def main() -> int:
    api_base = env("RUNWISE_API")
    sync_token = env("RUNWISE_SYNC_TOKEN")

    payload: dict = {}

    print("pulling garmin…")
    t0 = time.monotonic()
    payload["garmin"] = collect_garmin()
    print(f"  done in {time.monotonic() - t0:.1f}s")

    oura = collect_oura()
    if oura:
        payload["oura"] = oura

    print("pushing to worker…")
    push(api_base, sync_token, payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
