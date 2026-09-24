#!/usr/bin/env python3
"""Fetch OpenRouter models listed between the previous list and now.

New models are read from the OpenRouter models API, which returns every model
together with the unix timestamp at which it was added. The end of the last
list is stored in the manifest so the next run resumes where the previous one
stopped.
"""

import argparse
import csv
import datetime as dt
import http.client
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MODELS_URL = "https://openrouter.ai/api/v1/models"
DEFAULT_USER_AGENT = (
    "new-openrouter-models/1.0 (https://github.com/GHLists/new-openrouter-models)"
)

DESCRIPTION_LIMIT = 300
CSV_HEADER = (
    "created_at",
    "model",
    "name",
    "context_length",
    "prompt_price",
    "completion_price",
    "modality",
    "description",
)

TRANSIENT_ERRORS = (
    urllib.error.URLError,
    TimeoutError,
    json.JSONDecodeError,
    http.client.HTTPException,
    OSError,
)


def iso(moment):
    moment = moment.astimezone(dt.timezone.utc)
    if moment.microsecond:
        fraction = f"{moment.microsecond:06d}".rstrip("0")
        return moment.strftime("%Y-%m-%dT%H:%M:%S") + f".{fraction}Z"
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_timestamp(value):
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    moment = dt.datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc)


def timestamp_filename(moment):
    moment = moment.astimezone(dt.timezone.utc)
    stamp = moment.strftime("%Y-%m-%dT%H-%M-%S")
    if moment.microsecond:
        stamp += "-" + f"{moment.microsecond:06d}".rstrip("0")
    return stamp + "Z"


def parse_epoch(value):
    """Parse a unix timestamp as returned by the OpenRouter API."""
    return dt.datetime.fromtimestamp(int(value), dt.timezone.utc)


def fetch_json(url, user_agent, retries=3, backoff=5.0):
    last_error = None
    for attempt in range(1, retries + 1):
        request = urllib.request.Request(
            url,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except TRANSIENT_ERRORS as error:
            last_error = error
        if attempt < retries:
            print(f"attempt {attempt} failed ({last_error}), retrying", file=sys.stderr)
            time.sleep(backoff * attempt)
    raise RuntimeError(f"failed to fetch {url}: {last_error}")


def fetch_models(user_agent, retries):
    payload = fetch_json(MODELS_URL, user_agent, retries=retries)
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise RuntimeError("OpenRouter response has an invalid data field")
    return payload["data"]


def clean_text(value, limit=DESCRIPTION_LIMIT):
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "\u2026"
    return text


def price_value(value):
    text = str(value or "").strip()
    return text if text else ""


def build_row(model, created):
    pricing = model.get("pricing") or {}
    architecture = model.get("architecture") or {}
    context = model.get("context_length")
    return {
        "created_at": iso(created),
        "model": model.get("id") or "",
        "name": model.get("name") or "",
        "context_length": context if isinstance(context, int) else "",
        "prompt_price": price_value(pricing.get("prompt")),
        "completion_price": price_value(pricing.get("completion")),
        "modality": architecture.get("modality") or "",
        "description": clean_text(model.get("description")),
    }


def write_csv(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_HEADER)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def read_manifest_text(path):
    """Read the manifest from disk, or fall back to the committed copy.

    The workflow checks out only ``scripts`` from the repository, so the
    manifest can be missing from the working tree even though it is committed.
    """
    manifest_path = Path(path)
    try:
        return manifest_path.read_text(encoding="utf-8")
    except OSError:
        pass
    try:
        result = subprocess.run(
            ["git", "show", f"HEAD:{manifest_path.as_posix()}"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout


def load_manifest(path):
    text = read_manifest_text(path)
    if text is None:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"manifest {path} is not valid JSON") from error
    if not isinstance(data, dict):
        raise RuntimeError(f"manifest {path} must contain a JSON object")
    version = data.get("state_version", 1)
    if version != 1:
        raise RuntimeError(f"manifest {path} has an unsupported state version")
    return data


def save_manifest(path, manifest):
    manifest_path = Path(path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = manifest_path.with_name(f".{manifest_path.name}.tmp")
    text = json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, manifest_path)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since",
        help="UTC start timestamp as ISO 8601 (default: end of the last list)",
    )
    parser.add_argument(
        "--until",
        help="UTC end timestamp as ISO 8601 (default: now)",
    )
    parser.add_argument("--output-dir", default="data")
    parser.add_argument("--manifest", default="latest.json")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument(
        "--lookback-hours",
        type=float,
        default=1.0,
        help="window length when no previous list exists (default: 1)",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    now = dt.datetime.now(dt.timezone.utc)
    until = parse_timestamp(args.until) if args.until else now
    manifest = load_manifest(args.manifest)

    if args.since:
        since = parse_timestamp(args.since)
        if "window" in manifest:
            stored_window = parse_timestamp(manifest["window"])
            if since < stored_window:
                raise RuntimeError(
                    "backfill would move the window backwards; "
                    f"the manifest window is {iso(stored_window)}"
                )
    elif "window" in manifest:
        since = parse_timestamp(manifest["window"])
    else:
        since = until - dt.timedelta(hours=args.lookback_hours)

    if since >= until:
        print(f"nothing to do ({iso(since)} >= {iso(until)})", file=sys.stderr)
        return 0

    models = fetch_models(args.user_agent, args.retries)

    rows = []
    seen = set()
    skipped = 0
    for model in models:
        if not isinstance(model, dict):
            skipped += 1
            continue
        model_id = model.get("id")
        if not isinstance(model_id, str) or not model_id:
            skipped += 1
            continue
        try:
            created = parse_epoch(model["created"])
        except (KeyError, TypeError, ValueError):
            skipped += 1
            continue
        if created <= since or created > until:
            continue
        if model_id in seen:
            continue
        seen.add(model_id)
        rows.append(build_row(model, created))
    rows.sort(key=lambda row: row["created_at"])
    if skipped:
        print(f"skipped {skipped} malformed models", file=sys.stderr)

    manifest["window"] = iso(until)
    manifest["source_truncated"] = False
    if rows:
        output = Path(args.output_dir) / f"new-models-{timestamp_filename(until)}.csv"
        write_csv(output, rows)
        manifest["list"] = {
            "path": output.as_posix(),
            "from": iso(since),
            "to": iso(until),
            "count": len(rows),
        }
        print(
            f"wrote {len(rows)} models listed between {iso(since)} "
            f"and {iso(until)} to {output}"
        )
    else:
        print(f"no new models between {iso(since)} and {iso(until)}")
    save_manifest(args.manifest, manifest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
