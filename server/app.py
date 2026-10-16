"""Minimal sync server: receives gzip chunks, swaps the table atomically, serves the dashboard.

Standard library only. Run:  SYNC_TOKEN=... python server/app.py
"""
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import gzip
import hmac
import json
import os
import sqlite3
import threading

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("DB_PATH", ROOT / "server" / "dashboard.db"))
WEB_DIR = ROOT / "web"
SYNC_TOKEN = os.environ.get("SYNC_TOKEN", "")
MAX_BODY = 5 * 1024 * 1024  # bytes per request
PORT = int(os.environ.get("PORT", "8000"))

COLUMNS = ["lead_id", "created_at", "channel", "brand", "model",
           "contacted", "visited", "test_drive", "sold"]

_pending: dict[str, dict[int, list]] = {}  # batch_id -> {part: rows}
_lock = threading.Lock()


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS sync_log (
        synced_at TEXT, batch_id TEXT, rows INTEGER, parts INTEGER)""")
    return con


def swap_table(batch_id: str, parts: dict[int, list]) -> int:
    """Write all parts into a new table and replace the live one in a single transaction."""
    rows = [row for i in sorted(parts) for row in parts[i]]
    with db() as con:
        con.execute("DROP TABLE IF EXISTS leads_new")
        con.execute(f"CREATE TABLE leads_new ({', '.join(COLUMNS)})")
        con.executemany(f"INSERT INTO leads_new VALUES ({', '.join('?' * len(COLUMNS))})", rows)
        con.execute("DROP TABLE IF EXISTS leads")
        con.execute("ALTER TABLE leads_new RENAME TO leads")
        con.execute("INSERT INTO sync_log VALUES (?, ?, ?, ?)",
                    (datetime.now(timezone.utc).isoformat(timespec="seconds"), batch_id, len(rows), len(parts)))
    return len(rows)


def summary(brand: str | None) -> dict:
    where, params = ("WHERE brand = ?", [brand]) if brand else ("", [])
    with db() as con:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE name = 'leads'").fetchone():
            return {"empty": True}
        kpi = con.execute(f"""SELECT COUNT(*), SUM(contacted), SUM(visited), SUM(test_drive), SUM(sold)
                              FROM leads {where}""", params).fetchone()
        channels = con.execute(f"""SELECT channel, COUNT(*), SUM(sold) FROM leads {where}
                                   GROUP BY channel ORDER BY COUNT(*) DESC""", params).fetchall()
        monthly = con.execute(f"""SELECT SUBSTR(created_at, 1, 7) AS m, COUNT(*), SUM(sold) FROM leads {where}
                                  GROUP BY m ORDER BY m""", params).fetchall()
        brands = [r[0] for r in con.execute("SELECT DISTINCT brand FROM leads ORDER BY brand")]
        last = con.execute("SELECT synced_at, rows FROM sync_log ORDER BY synced_at DESC LIMIT 1").fetchone()
    leads, contacted, visited, test_drive, sold = (v or 0 for v in kpi)
    return {
        "brands": brands,
        "last_sync": {"at": last[0], "rows": last[1]} if last else None,
        "funnel": [["Lead", leads], ["Contacted", contacted], ["Showroom visit", visited],
                   ["Test drive", test_drive], ["Sale", sold]],
        "channels": [{"channel": c, "leads": n, "sales": s} for c, n, s in channels],
        "monthly": [{"month": m, "leads": n, "sales": s} for m, n, s in monthly],
    }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/api/summary":
            brand = parse_qs(url.query).get("brand", [None])[0]
            return self.send_json(200, summary(brand if brand and brand != "All" else None))
        return super().do_GET()

    def do_POST(self):
        if urlparse(self.path).path != "/api/upload":
            return self.send_json(404, {"error": "not found"})
        if not SYNC_TOKEN or not hmac.compare_digest(self.headers.get("X-Sync-Token", ""), SYNC_TOKEN):
            return self.send_json(401, {"error": "bad token"})
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_BODY:
            return self.send_json(413, {"error": "chunk too large"})
        try:
            batch_id = self.headers["X-Batch-Id"]
            part, total = int(self.headers["X-Part"]), int(self.headers["X-Total-Parts"])
            payload = json.loads(gzip.decompress(self.rfile.read(length)))
            if payload["columns"] != COLUMNS:
                return self.send_json(400, {"error": f"expected columns {COLUMNS}"})
        except (KeyError, ValueError, OSError) as exc:
            return self.send_json(400, {"error": str(exc)})

        with _lock:
            parts = _pending.setdefault(batch_id, {})
            parts[part] = payload["rows"]
            complete = len(parts) == total
            if complete:
                _pending.pop(batch_id)
        if complete:
            rows = swap_table(batch_id, parts)
            return self.send_json(200, {"status": "swapped", "rows": rows})
        return self.send_json(200, {"status": "received", "part": part, "of": total})


if __name__ == "__main__":
    if not SYNC_TOKEN:
        raise SystemExit("Set the SYNC_TOKEN environment variable first.")
    print(f"Dashboard: http://localhost:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
