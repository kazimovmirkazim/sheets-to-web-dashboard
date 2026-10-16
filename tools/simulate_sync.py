"""Local stand-in for apps_script/Code.gs: same hash check, same gzip chunks, same headers.

Generates synthetic leads (200K+ rows by default) so the whole flow runs without a Google account.

    python tools/simulate_sync.py              # first run uploads, second run skips (no changes)
    python tools/simulate_sync.py --seed 2     # different data -> uploads again
"""
from pathlib import Path
import argparse
import gzip
import hashlib
import json
import os
import time
import urllib.request
import uuid

import numpy as np
import pandas as pd

COLUMNS = ["lead_id", "created_at", "channel", "brand", "model",
           "contacted", "visited", "test_drive", "sold"]
STATE_FILE = Path(__file__).resolve().parent / ".last_hash"

CHANNELS = ["Instagram", "Facebook", "Turbo.az", "Call center", "Google Ads", "Website form", "Walk-in", "Referral"]
CHANNEL_P = [0.26, 0.19, 0.13, 0.15, 0.12, 0.09, 0.035, 0.025]
VISIT_P = [0.18, 0.17, 0.25, 0.45, 0.32, 0.33, 1.00, 0.57]
MODELS = {"Toyota": ["Corolla", "RAV4", "bZ4X"], "Hyundai": ["i20", "Tucson", "Ioniq 5"],
          "Mitsubishi": ["Outlander", "L200"]}  # real model names, synthetic numbers


def synthetic_rows(n: int, seed: int) -> list[list]:
    rng = np.random.default_rng(seed)
    ch = rng.choice(len(CHANNELS), size=n, p=CHANNEL_P)
    pairs = [(b, m) for b, ms in MODELS.items() for m in ms]
    mi = rng.integers(0, len(pairs), size=n)
    # Seasonal demand: busier in spring and autumn.
    season = np.tile([0.85, 0.90, 1.10, 1.15, 1.05, 0.95, 0.85, 0.90, 1.10, 1.15, 1.05, 1.00], 2)
    dates = pd.date_range("2024-01-01", periods=730, freq="D")
    weights = season[(dates.year - 2024) * 12 + dates.month - 1]
    days = rng.choice(730, size=n, p=weights / weights.sum())
    contacted = rng.random(n) < 0.86
    visited = contacted & (rng.random(n) < np.array(VISIT_P)[ch])
    test_drive = visited & (rng.random(n) < 0.6)
    sold = test_drive & (rng.random(n) < 0.42)
    df = pd.DataFrame({
        "lead_id": [f"L{i:07d}" for i in range(1, n + 1)],
        "created_at": (pd.Timestamp("2024-01-01") + pd.to_timedelta(days, unit="D")).strftime("%Y-%m-%d"),
        "channel": np.array(CHANNELS)[ch],
        "brand": [pairs[i][0] for i in mi],
        "model": [pairs[i][1] for i in mi],
        "contacted": contacted.astype(int), "visited": visited.astype(int),
        "test_drive": test_drive.astype(int), "sold": sold.astype(int),
    })
    return df[COLUMNS].values.tolist()


def post(url: str, token: str, body: bytes, batch_id: str, part: int, total: int) -> dict:
    req = urllib.request.Request(url + "/api/upload", data=body, method="POST", headers={
        "Content-Type": "application/gzip", "X-Sync-Token": token,
        "X-Batch-Id": batch_id, "X-Part": str(part), "X-Total-Parts": str(total),
    })
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=210_000)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--chunk", type=int, default=20_000)
    ap.add_argument("--url", default=os.environ.get("SERVER_URL", "http://localhost:8000"))
    args = ap.parse_args()
    token = os.environ["SYNC_TOKEN"]

    rows = synthetic_rows(args.rows, args.seed)
    raw = json.dumps([COLUMNS, rows], default=int).encode()
    digest = hashlib.sha256(raw).hexdigest()
    if STATE_FILE.exists() and STATE_FILE.read_text() == digest:
        print("No changes since last sync, skipped.")
        return

    started = time.perf_counter()
    batch_id, total = str(uuid.uuid4()), -(-len(rows) // args.chunk)
    sent = 0
    for part in range(total):
        chunk = rows[part * args.chunk:(part + 1) * args.chunk]
        body = gzip.compress(json.dumps({"columns": COLUMNS, "rows": chunk}, default=int).encode())
        sent += len(body)
        result = post(args.url, token, body, batch_id, part, total)
    STATE_FILE.write_text(digest)
    print(f"{len(rows):,} rows, {total} parts, {len(raw) / 1e6:.1f} MB raw -> {sent / 1e6:.1f} MB gzip, "
          f"{time.perf_counter() - started:.1f}s. Server: {result}")


if __name__ == "__main__":
    main()
