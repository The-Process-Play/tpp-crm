"""Tests for the CRM data layer, Claude connector and web API.

Runs against SQLite always, and against Postgres too when TEST_DATABASE_URL is set
(the CI workflow does this with a Postgres service container):

    python3 -m unittest discover -s tests -v
    TEST_DATABASE_URL=postgresql://... python3 -m unittest discover -s tests -v

WARNING: the Postgres run drops and recreates all CRM tables in TEST_DATABASE_URL.
Never point it at a real database.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from crm import db, import_hubspot, mcp_server  # noqa: E402

TABLES = ["deal_history", "activities", "contacts", "deals", "companies", "stages"]


class Base:
    """Mixin with the actual tests; subclasses pick the database."""

    def setUp(self):
        self.conn = db.connect()
        db.init_db(self.conn)
        self.conn.close()
        self.conn = db.connect()
        self.seed()

    def tearDown(self):
        self.conn.close()

    def seed(self):
        c = self.conn
        self.a = db.create_deal(c, "Acme - ITSM Rollout", stage="Negotiation", amount=100000,
                                close_date="2020-01-31", owner="Ben Clark", user="t")
        self.b = db.create_deal(c, "Acme - HR Service Delivery", stage="Prospect", amount=50000,
                                close_date="2099-06-30", owner="Raj Verma", user="t")
        self.w = db.create_deal(c, "Globex - Platform Health Check", stage="Close Won", amount=30000,
                                owner="Ben Clark", user="t")
        self.l = db.create_deal(c, "Initech - CMDB 50%_Off", stage="Closed lost", amount=20000, user="t")

    # ---- deals
    def test_create_infers_company_and_logs(self):
        self.assertEqual(self.a["company"], "Acme")
        self.assertEqual(self.a["stage_category"], "open")
        self.assertEqual(self.a["history"][0]["field"], "created")

    def test_company_names_are_case_insensitive(self):
        d = db.create_deal(self.conn, "ACME - Something", user="t")
        self.assertEqual(d["company"], "Acme")

    def test_update_tracks_history_and_aliases(self):
        d = db.update_deal(self.conn, self.b["id"], user="alex", stage="won", amount="£60,000", owner="")
        self.assertEqual(d["stage"], "Close Won")
        self.assertEqual(d["amount"], 60000)
        fields = {h["field"]: h for h in d["history"]}
        self.assertEqual(fields["stage"]["old_value"], "Prospect")
        self.assertEqual(fields["stage"]["changed_by"], "alex")
        self.assertEqual(fields["owner"]["new_value"], None)

    def test_update_noop_records_nothing(self):
        before = len(db.get_deal(self.conn, self.a["id"])["history"])
        db.update_deal(self.conn, self.a["id"], stage="Negotiation", amount=100000)
        self.assertEqual(len(db.get_deal(self.conn, self.a["id"])["history"]), before)

    def test_invalid_input_rejected(self):
        with self.assertRaises(db.CRMError):
            db.update_deal(self.conn, self.a["id"], stage="Bogus")
        with self.assertRaises(db.CRMError):
            db.update_deal(self.conn, self.a["id"], close_date="31/01/2026")
        with self.assertRaises(db.CRMError):
            db.update_deal(self.conn, self.a["id"], amount=-5)
        with self.assertRaises(db.CRMError):
            db.create_deal(self.conn, "  ")

    def test_find_deal_ambiguous_and_missing(self):
        with self.assertRaises(db.CRMError) as cm:
            db.get_deal(self.conn, "Acme")
        self.assertIn("matches 2 deals", str(cm.exception))
        with self.assertRaises(db.CRMError):
            db.get_deal(self.conn, "Nope Ltd")
        self.assertEqual(db.get_deal(self.conn, "itsm rollout")["id"], self.a["id"])

    def test_search_filters(self):
        c = self.conn
        self.assertEqual(len(db.search_deals(c, company="acme")), 2)
        self.assertEqual([d["name"] for d in db.search_deals(c, category="open", sort="amount")],
                         ["Acme - ITSM Rollout", "Acme - HR Service Delivery"])
        self.assertEqual(len(db.search_deals(c, owner="ben")), 2)
        self.assertEqual(len(db.search_deals(c, min_amount=60000)), 1)
        self.assertEqual(len(db.search_deals(c, close_from="2099-01-01")), 1)

    def test_like_wildcards_are_literal(self):
        self.assertEqual(len(db.search_deals(self.conn, query="50%_")), 1)
        self.assertEqual(len(db.search_deals(self.conn, query="%")), 1)
        self.assertEqual(len(db.search_deals(self.conn, query="_")), 1)

    def test_delete(self):
        db.delete_deal(self.conn, self.l["id"])
        self.assertEqual(len(db.search_deals(self.conn)), 3)

    # ---- activities
    def test_activities_and_tasks(self):
        c = self.conn
        db.add_activity(c, "Called them", type="call", deal=self.a["id"], author="alex")
        t = db.add_activity(c, "Send SoW", type="task", deal=self.a["id"], due_date="2026-01-01", author="alex")
        self.assertEqual(len(db.get_deal(c, self.a["id"])["activities"]), 2)
        self.assertEqual([x["body"] for x in db.list_tasks(c)], ["Send SoW"])
        db.complete_task(c, t["id"])
        self.assertEqual(db.list_tasks(c), [])
        self.assertEqual(len(db.list_tasks(c, include_done=True)), 1)
        with self.assertRaises(db.CRMError):
            db.complete_task(c, 999999)
        with self.assertRaises(db.CRMError):
            db.add_activity(c, "x", type="fax")

    # ---- companies & contacts
    def test_companies_and_contacts(self):
        c = self.conn
        db.add_contact(c, "Jane Doe", company="Acme", email="jane@example.com")
        co = db.get_company(c, "acme")
        self.assertEqual(len(co["deals"]), 2)
        self.assertEqual(co["contacts"][0]["name"], "Jane Doe")
        self.assertEqual(db.search_contacts(c, "jane")[0]["company"], "Acme")
        listed = {r["name"]: r for r in db.list_companies(c)}
        self.assertEqual(listed["Acme"]["open_value"], 150000)
        self.assertEqual(listed["Globex"]["won_value"], 30000)
        db.update_company(c, "Globex", new_name="Globex Corp", industry="Energy")
        self.assertEqual(db.get_company(c, "Globex Corp")["industry"], "Energy")

    # ---- reporting
    def test_pipeline_summary(self):
        s = db.pipeline_summary(self.conn)
        self.assertEqual(s["open_pipeline_value"], 150000)
        self.assertEqual(s["weighted_pipeline_value"], 100000 * 0.75 + 50000 * 0.10)
        self.assertEqual(s["won_value"], 30000)
        self.assertEqual(s["win_rate_pct"], 50.0)
        self.assertEqual((s["past_close_date_deals"], s["past_close_date_value"]), (1, 100000))
        owners = {r["owner"]: r for r in s["by_owner"]}
        self.assertEqual(owners["Ben Clark"]["open_deals"], 1)
        self.assertEqual(owners["(unassigned)"]["lost_deals"], 1)
        self.assertEqual(db.pipeline_summary(self.conn, owner="raj")["open_pipeline_value"], 50000)

    def test_stale_deals(self):
        self.assertEqual([d["id"] for d in db.stale_deals(self.conn)], [self.a["id"]])

    def test_read_only_query(self):
        r = db.read_only_query("SELECT COUNT(*) AS n FROM deals")
        self.assertEqual(r["rows"][0][0], 4)
        for bad in ("DELETE FROM deals", "SELECT 1; DELETE FROM deals", "UPDATE deals SET amount = 0"):
            with self.assertRaises(db.CRMError):
                db.read_only_query(bad)
        with self.assertRaises(db.CRMError):  # blocked by the read-only connection, not just the prefix check
            db.read_only_query("WITH x AS (DELETE FROM deals RETURNING id) SELECT * FROM x")
        self.assertEqual(len(db.search_deals(self.conn)), 4)

    # ---- importer
    def test_import_csv_is_repeatable(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "deals.csv")
            with open(path, "w") as f:
                f.write("Record ID,Deal Name,Deal Stage,Close Date,Deal owner,Amount\n"
                        "111,Umbrella - Discovery,Prospect,2026-03-01 09:00,Jo Bloggs (Deactivated User),\"12,500\"\n"
                        "222,Standalone Co,Weird Stage,,,\n")
            import_hubspot.main([path])
            import_hubspot.main([path])
            rows = db.search_deals(self.conn, query="Umbrella")
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0]["company"], rows[0]["owner"], rows[0]["amount"], rows[0]["close_date"]),
                             ("Umbrella", "Jo Bloggs", 12500, "2026-03-01"))
            self.assertEqual(db.search_deals(self.conn, query="Standalone")[0]["stage"], "Weird Stage")
        finally:
            shutil.rmtree(tmp)

    # ---- Claude connector
    def test_mcp_protocol(self):
        init = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(init["protocolVersion"], "2025-06-18")
        names = [t["name"] for t in mcp_server.handle({"id": 2, "method": "tools/list"})["tools"]]
        self.assertIn("update_deal", names)
        ok = mcp_server.handle({"id": 3, "method": "tools/call",
                                "params": {"name": "search_deals", "arguments": {"company": "Acme"}}})
        self.assertFalse(ok["isError"])
        self.assertEqual(len(json.loads(ok["content"][0]["text"])), 2)
        bad = mcp_server.handle({"id": 4, "method": "tools/call",
                                 "params": {"name": "update_deal", "arguments": {"deal": "Acme"}}})
        self.assertTrue(bad["isError"])

    # ---- web API
    def test_web_api(self):
        from crm import web
        srv = web.make_server("127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % srv.server_address[1]

        def call(path, body=None, ctype="application/json", headers=None):
            req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                         headers=dict({"Content-Type": ctype}, **(headers or {})))
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())
        try:
            self.assertEqual(call("/healthz"), (200, {"status": "ok"}))
            status, deals = call("/api/deals?owner=Ben")
            self.assertEqual((status, len(deals)), (200, 2))
            status, d = call("/api/deal/%d" % self.b["id"], {"stage": "Negotiation"})
            self.assertEqual((status, d["stage"]), (200, "Negotiation"))
            self.assertEqual(d["history"][0]["changed_by"], "web")
            status, _ = call("/api/deal/%d" % self.b["id"], {"stage": "Hold"}, ctype="text/plain")
            self.assertEqual(status, 400)  # cross-site form posts are refused
            status, err = call("/api/deal/%d" % self.b["id"], {"stage": "Bogus"})
            self.assertEqual(status, 400)
            self.assertIn("Unknown stage", err["error"])
            # Sign-in header is ignored unless TRUST_EASY_AUTH is on
            status, d = call("/api/deal/%d" % self.b["id"], {"stage": "Prospect"},
                             headers={"X-MS-CLIENT-PRINCIPAL-NAME": "spoof@example.com"})
            self.assertEqual(d["history"][0]["changed_by"], "web")
            os.environ["TRUST_EASY_AUTH"] = "1"
            try:
                status, d = call("/api/deal/%d" % self.b["id"], {"stage": "Hold"},
                                 headers={"X-MS-CLIENT-PRINCIPAL-NAME": "jo@example.com"})
                self.assertEqual(d["history"][0]["changed_by"], "jo@example.com")
                status, _ = call("/api/deals?owner=Ben", headers={"X-MS-CLIENT-PRINCIPAL-NAME": "jo@example.com"})
                self.assertEqual(status, 200)
                req = urllib.request.Request(base + "/api/meta")
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    urllib.request.urlopen(req)  # no sign-in header -> refused
                self.assertEqual(cm.exception.code, 401)
            finally:
                del os.environ["TRUST_EASY_AUTH"]
        finally:
            srv.shutdown()
            srv.server_close()


class SQLiteTests(Base, unittest.TestCase):
    def setUp(self):
        self._env = os.environ.pop("DATABASE_URL", None)
        self.tmp = tempfile.mkdtemp()
        self._old = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmp, "test.db")
        super().setUp()

    def tearDown(self):
        super().tearDown()
        db.DB_PATH = self._old
        shutil.rmtree(self.tmp)
        if self._env:
            os.environ["DATABASE_URL"] = self._env


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "set TEST_DATABASE_URL to run Postgres tests")
class PostgresTests(Base, unittest.TestCase):
    def setUp(self):
        self._env = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
        os.environ.pop("READONLY_DATABASE_URL", None)
        import psycopg
        with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as c:
            for t in TABLES:
                c.execute("DROP TABLE IF EXISTS %s CASCADE" % t)
        super().setUp()

    def tearDown(self):
        super().tearDown()
        if self._env:
            os.environ["DATABASE_URL"] = self._env
        else:
            os.environ.pop("DATABASE_URL", None)


if __name__ == "__main__":
    unittest.main()
