"""Web app for the CRM: pipeline board, deal list, tasks, company view and quick edits.

Local use:
    python3 crm/web.py                 # then open http://127.0.0.1:8765

Hosted (Azure App Service container), configured by environment variables:
    HOST=0.0.0.0  PORT=8000            # where to listen
    DATABASE_URL=postgresql://...      # Postgres instead of the local SQLite file
    TRUST_EASY_AUTH=1                  # take the signed-in user from App Service Authentication
                                       # (Microsoft Entra ID) and refuse requests without one

Standard library only (plus psycopg when DATABASE_URL is set).
"""
import argparse
import json
import os
import re
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from crm import db  # noqa: E402

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
STATIC = os.path.join(STATIC_DIR, "index.html")
STATIC_TYPES = {".svg": "image/svg+xml", ".woff2": "font/woff2", ".txt": "text/plain; charset=utf-8"}
STATIC_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
UPDATABLE = ("name", "company", "stage", "amount", "close_date", "owner", "next_step")
EASY_AUTH_HEADER = "X-MS-CLIENT-PRINCIPAL-NAME"
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                               "style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'",
}


class Unauthorised(Exception):
    pass


def trust_easy_auth():
    return os.environ.get("TRUST_EASY_AUTH") == "1"


class Handler(BaseHTTPRequestHandler):
    server_version = "TPPCRM/1.1"

    def log_message(self, fmt, *args):  # quieter console: method, path, status
        sys.stderr.write("%s %s %s\n" % (self.command, self.path.split("?")[0], args[1] if len(args) > 1 else ""))

    def _send(self, status, body, ctype="application/json", cache=None):
        data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, cache if (k == "Cache-Control" and cache) else v)
        self.end_headers()
        self.wfile.write(data)

    def _user(self):
        """Who is making this request, for the audit trail.

        Behind App Service Authentication, the platform strips any client-sent copy of the
        principal header and sets it from the verified sign-in, so it can be trusted only when
        TRUST_EASY_AUTH=1. Locally, TPP_CRM_USER (or "web") is used.
        """
        if trust_easy_auth():
            name = (self.headers.get(EASY_AUTH_HEADER) or "").strip()
            if not name:
                raise Unauthorised()
            return name
        return os.environ.get("TPP_CRM_USER") or "web"

    def _json_body(self):
        # Requiring a JSON content type blocks simple cross-site form posts from other web pages.
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            raise db.CRMError("Content-Type must be application/json")
        length = int(self.headers.get("Content-Length") or 0)
        if length > 100000:
            raise db.CRMError("Request too large")
        body = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(body, dict):
            raise db.CRMError("Expected a JSON object")
        return body

    def _route(self, method):
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        conn = None
        try:
            if method == "GET" and parts == ["healthz"]:
                conn = db.connect()
                conn.execute("SELECT 1").fetchone()
                return self._send(200, {"status": "ok"})
            user = self._user()
            if method == "GET" and not parts:
                with open(STATIC, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if method == "GET" and parts[:1] == ["static"]:
                return self._static(parts[1:])
            if parts[:1] != ["api"]:
                return self._send(404, {"error": "not found"})
            api = parts[1:]
            conn = db.connect()
            if method == "GET" and api == ["me"]:
                return self._send(200, {"user": user})
            if method == "GET" and api == ["meta"]:
                owners = [r["owner"] for r in conn.execute(
                    "SELECT DISTINCT owner FROM deals WHERE owner IS NOT NULL ORDER BY owner")]
                stages = [dict(r) for r in conn.execute("SELECT * FROM stages ORDER BY sort_order")]
                return self._send(200, {"stages": stages, "owners": owners})
            if method == "GET" and api == ["deals"]:
                allowed = {"query", "stage", "category", "owner", "company", "sort", "limit"}
                return self._send(200, db.search_deals(conn, **{k: v for k, v in q.items() if k in allowed and v}))
            if method == "GET" and api == ["summary"]:
                return self._send(200, db.pipeline_summary(conn, owner=q.get("owner") or None))
            if method == "GET" and len(api) == 2 and api[0] == "deal":
                return self._send(200, db.get_deal(conn, int(api[1])))
            if method == "POST" and api == ["deals"]:
                body = self._json_body()
                return self._send(201, db.create_deal(conn, user=user, **{k: body.get(k) for k in UPDATABLE
                                                                          if body.get(k) not in (None, "")}))
            if method == "POST" and len(api) == 2 and api[0] == "deal":
                body = self._json_body()
                return self._send(200, db.update_deal(conn, int(api[1]), user=user,
                                                      **{k: body[k] for k in UPDATABLE if k in body}))
            if method == "POST" and len(api) == 3 and api[0] == "deal" and api[2] == "activity":
                body = self._json_body()
                return self._send(201, db.add_activity(conn, body.get("body"), type=body.get("type", "note"),
                                                       deal=int(api[1]), due_date=body.get("due_date") or None,
                                                       author=user))
            if method == "GET" and api == ["tasks"]:
                return self._send(200, db.list_tasks(conn, owner=q.get("owner") or None,
                                                     include_done=q.get("include_done") == "1"))
            if method == "POST" and len(api) == 3 and api[0] == "task" and api[2] == "done":
                body = self._json_body()
                return self._send(200, db.complete_task(conn, int(api[1]), bool(body.get("done", True))))
            if method == "GET" and api == ["company"]:
                return self._send(200, db.get_company(conn, q.get("name", "")))
            if method == "POST" and api == ["contacts"]:
                body = self._json_body()
                return self._send(201, db.add_contact(conn, body.get("name"), company=body.get("company"),
                                                      email=body.get("email") or None, phone=body.get("phone") or None,
                                                      title=body.get("title") or None))
            return self._send(404, {"error": "not found"})
        except Unauthorised:
            return self._send(401, {"error": "Sign-in required"})
        except (db.CRMError, ValueError, TypeError) as e:
            if conn is not None:
                conn.rollback()
            return self._send(400, {"error": str(e)})
        except Exception:
            traceback.print_exc(file=sys.stderr)  # details go to the log, not the browser
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
            return self._send(500, {"error": "Something went wrong. Please try again."})
        finally:
            if conn is not None:
                conn.close()

    def _static(self, parts):
        """Serve brand assets (logo, icon, fonts). Only plain file names in known folders."""
        if not (1 <= len(parts) <= 2) or (len(parts) == 2 and parts[0] != "fonts"):
            return self._send(404, {"error": "not found"})
        name = parts[-1]
        ctype = STATIC_TYPES.get(os.path.splitext(name)[1])
        if not ctype or not STATIC_NAME.match(name):
            return self._send(404, {"error": "not found"})
        path = os.path.join(STATIC_DIR, *parts)
        if not os.path.isfile(path):
            return self._send(404, {"error": "not found"})
        with open(path, "rb") as f:
            return self._send(200, f.read(), ctype, cache="public, max-age=86400")

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")


def make_server(host, port):
    return ThreadingHTTPServer((host, port), Handler)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
    args = ap.parse_args()
    if os.environ.get("REQUIRE_DATABASE_URL") == "1" and not db.database_url():
        sys.exit("DATABASE_URL is not set. The hosted app must use Postgres, not a local file.")
    conn = db.connect()
    db.init_db(conn)
    conn.close()
    srv = make_server(args.host, args.port)
    where = "Postgres" if db.database_url() else db.DB_PATH
    print("TPP CRM running at http://%s:%d  (data: %s; sign-in: %s)  Ctrl+C to stop" % (
        args.host, args.port, where, "App Service" if trust_easy_auth() else "off, local only"), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
