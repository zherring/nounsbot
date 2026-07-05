"""Serve the site from the bot process (Railway + nounsvote.com).

Static files come from docs/; /verdicts.json is generated live from the DB so
the record is always current without waiting for a redeploy. The git-commit
publisher still runs — that's the public audit trail; this is just freshness.
"""

import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from . import db, publisher
from .config import REPO_ROOT

DOCS = REPO_ROOT / "docs"


class SiteHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] in ("/verdicts.json", "/health"):
            try:
                if self.path.startswith("/health"):
                    body = b'{"ok": true}'
                else:
                    conn = db.connect()  # per-request: sqlite is not cross-thread
                    payload = publisher.build_payload(conn)
                    conn.close()
                    if not payload["verdicts"]:
                        return super().do_GET()  # fresh DB: fall back to committed file
                    body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            except Exception:
                pass  # fall through to the static copy
        super().do_GET()

    def log_message(self, fmt, *args):
        pass  # keep the poller's stdout readable


def start(port: int) -> None:
    handler = partial(SiteHandler, directory=str(DOCS))
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="web")
    thread.start()
    print(f"site serving on :{port}")
