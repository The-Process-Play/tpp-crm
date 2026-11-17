"""Core data layer for the TPP CRM.

Runs on two databases with the same code:
  * SQLite (default): a local file, standard library only, runs on macOS system Python 3.9+.
  * PostgreSQL: used when DATABASE_URL is set (e.g. Azure Database for PostgreSQL).
    Needs `psycopg` (see requirements.txt).

Both the Claude connector (mcp_server.py) and the web app (web.py) go through these
functions, so all business rules live here. SQL is written once in a portable subset;
the small Connection wrapper below adapts it for Postgres (placeholders, LIKE -> ILIKE).
Timestamps are stored as UTC text 'YYYY-MM-DD HH:MM:SS' and dates as 'YYYY-MM-DD' in both
databases, so comparisons and output are identical.
"""
import datetime as _dt
import os
import re
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("TPP_CRM_DB", os.path.join(ROOT, "data", "crm.db"))


def database_url():
    return os.environ.get("DATABASE_URL") or None


# Pipeline stages, in order. `category` drives reporting:
#   open   = live pipeline, counted in forecasts
#   won / lost = closed
#   parked = not actively pursued (newsletter list, ignore, hold)
STAGES = [
    ("Prospect", "open", 10),
    ("To Chase", "open", 20),
    ("Awaiting More Information", "open", 30),
    ("In Discussion", "open", 40),
    ("Workshop/Strategy", "open", 50),
    ("Negotiation", "open", 60),
    ("Close Won", "won", 100),
    ("Closed lost", "lost", 110),
    ("Hold", "parked", 120),
    ("Add to monthly newsletter list", "parked", 130),
    ("Ignore", "parked", 140),
]
STAGE_NAMES = [s[0] for s in STAGES]

PROBABILITY = {
    "Prospect": 10, "To Chase": 15, "Awaiting More Information": 25,
    "In Discussion": 35, "Workshop/Strategy": 50, "Negotiation": 75,
    "Close Won": 100,
}

TRACKED_FIELDS = ("stage", "amount", "close_date", "owner", "company_id", "name")

# {ID}, {REAL} and {NOW} are filled in per database by _schema().
SCHEMA_TEMPLATE = """
CREATE TABLE IF NOT EXISTS stages (
    name        TEXT PRIMARY KEY,
    category    TEXT NOT NULL CHECK (category IN ('open','won','lost','parked')),
    sort_order  INTEGER NOT NULL,
    probability INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS companies (
    id         {ID},
    name       TEXT NOT NULL,
    website    TEXT,
    industry   TEXT,
    notes      TEXT,
    created_at TEXT NOT NULL DEFAULT {NOW}
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_companies_name ON companies (lower(name));

CREATE TABLE IF NOT EXISTS contacts (
    id         {ID},
    company_id BIGINT REFERENCES companies(id) ON DELETE SET NULL,
    name       TEXT NOT NULL,
    email      TEXT,
    phone      TEXT,
    title      TEXT,
    notes      TEXT,
    created_at TEXT NOT NULL DEFAULT {NOW}
);

CREATE TABLE IF NOT EXISTS deals (
    id          {ID},
    hubspot_id  TEXT UNIQUE,
    name        TEXT NOT NULL,
    company_id  BIGINT REFERENCES companies(id) ON DELETE SET NULL,
    stage       TEXT NOT NULL REFERENCES stages(name),
    amount      {REAL},
    close_date  TEXT,
    owner       TEXT,
    next_step   TEXT,
    created_at  TEXT NOT NULL DEFAULT {NOW},
    updated_at  TEXT NOT NULL DEFAULT {NOW}
);

CREATE TABLE IF NOT EXISTS activities (
    id         {ID},
    deal_id    BIGINT REFERENCES deals(id) ON DELETE CASCADE,
    company_id BIGINT REFERENCES companies(id) ON DELETE CASCADE,
    contact_id BIGINT REFERENCES contacts(id) ON DELETE SET NULL,
    type       TEXT NOT NULL CHECK (type IN ('note','call','meeting','email','task')),
    body       TEXT NOT NULL,
    due_date   TEXT,
    done       INTEGER NOT NULL DEFAULT 0,
    author     TEXT,
    created_at TEXT NOT NULL DEFAULT {NOW}
);

CREATE TABLE IF NOT EXISTS deal_history (
    id         {ID},
    deal_id    BIGINT NOT NULL REFERENCES deals(id) ON DELETE CASCADE,
    field      TEXT NOT NULL,
    old_value  TEXT,
    new_value  TEXT,
    changed_by TEXT,
    changed_at TEXT NOT NULL DEFAULT {NOW}
);

CREATE INDEX IF NOT EXISTS idx_deals_stage   ON deals(stage);
CREATE INDEX IF NOT EXISTS idx_deals_owner   ON deals(owner);
CREATE INDEX IF NOT EXISTS idx_deals_company ON deals(company_id);
CREATE INDEX IF NOT EXISTS idx_act_deal      ON activities(deal_id);
CREATE INDEX IF NOT EXISTS idx_hist_deal     ON deal_history(deal_id)
"""


class CRMError(ValueError):
    """Raised for invalid input; message is safe to show to the user."""


def _now():
    return _dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def _today():
    return _dt.date.today().isoformat()


# ---------------------------------------------------------------- connection

def _dict_factory(cursor, row):
    return {d[0]: row[i] for i, d in enumerate(cursor.description)}


class Connection:
    """Thin wrapper so the rest of the module can use one SQL dialect.

    Write SQL with `?` placeholders and LIKE; on Postgres these become `%s` and ILIKE
    (SQLite's LIKE is already case-insensitive). Rows are always plain dicts.
    """

    def __init__(self, raw, dialect):
        self.raw = raw
        self.dialect = dialect

    def _sql(self, sql):
        if self.dialect == "postgres":
            sql = sql.replace("%", "%%").replace("?", "%s")
            sql = re.sub(r"\bLIKE\b", "ILIKE", sql)
        return sql

    def execute(self, sql, args=()):
        return self.raw.execute(self._sql(sql), tuple(args))

    def insert(self, sql, args=()):
        """Run an INSERT and return the new row's id."""
        return self.execute(sql + " RETURNING id", args).fetchone()["id"]

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        self.raw.close()


def connect(path=None):
    url = database_url()
    if url and path is None:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError:
            raise RuntimeError("DATABASE_URL is set but psycopg is not installed: pip install -r requirements.txt")
        return Connection(psycopg.connect(url, row_factory=dict_row), "postgres")
    raw = sqlite3.connect(path or DB_PATH, timeout=10)
    raw.row_factory = _dict_factory
    raw.execute("PRAGMA foreign_keys = ON")
    raw.execute("PRAGMA journal_mode = WAL")
    return Connection(raw, "sqlite")


def _schema(dialect):
    if dialect == "postgres":
        parts = {"ID": "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY", "REAL": "DOUBLE PRECISION",
                 "NOW": "(to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS'))"}
    else:
        parts = {"ID": "INTEGER PRIMARY KEY", "REAL": "REAL", "NOW": "(datetime('now'))"}
    sql = SCHEMA_TEMPLATE
    for k, v in parts.items():
        sql = sql.replace("{%s}" % k, v)
    return [s.strip() for s in sql.split(";") if s.strip()]


def init_db(conn):
    for stmt in _schema(conn.dialect):
        conn.raw.execute(stmt)  # raw: DDL has no placeholders
    for name, cat, order in STAGES:
        conn.execute(
            "INSERT INTO stages(name, category, sort_order, probability) VALUES (?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET category=excluded.category, sort_order=excluded.sort_order",
            (name, cat, order, PROBABILITY.get(name, 0)))
    conn.commit()


# ---------------------------------------------------------------- helpers

def _rows(cur):
    return [dict(r) for r in cur.fetchall()]


def _norm_date(value):
    if value in (None, ""):
        return None
    try:
        return _dt.date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        raise CRMError("Dates must be YYYY-MM-DD, got %r" % value)


def _norm_amount(value):
    if value in (None, ""):
        return None
    try:
        amt = float(str(value).replace(",", "").replace("£", "").strip())
    except ValueError:
        raise CRMError("Amount must be a number, got %r" % value)
    if amt < 0:
        raise CRMError("Amount cannot be negative")
    return amt


def _like(text):
    """Wrap user text for a 'contains' LIKE match, escaping LIKE wildcards."""
    return "%" + str(text).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


LIKE = " LIKE ? ESCAPE '\\'"


def resolve_stage(conn, stage):
    """Accept a stage name case-insensitively, or a few common aliases."""
    if not stage:
        raise CRMError("Stage is required")
    aliases = {"won": "Close Won", "closed won": "Close Won", "lost": "Closed lost",
               "newsletter": "Add to monthly newsletter list"}
    key = stage.strip().lower()
    if key in aliases:
        return aliases[key]
    names = [r["name"] for r in conn.execute("SELECT name FROM stages").fetchall()] or STAGE_NAMES
    for name in names:
        if name.lower() == key:
            return name
    raise CRMError("Unknown stage %r. Valid stages: %s" % (stage, ", ".join(STAGE_NAMES)))


def get_or_create_company(conn, name):
    name = (name or "").strip()
    if not name:
        return None
    row = conn.execute("SELECT id FROM companies WHERE lower(name) = lower(?)", (name,)).fetchone()
    if row:
        return row["id"]
    return conn.insert("INSERT INTO companies(name, created_at) VALUES (?, ?)", (name, _now()))


def find_deal(conn, ref):
    """Find one deal by numeric id, HubSpot id, or (unique) partial name."""
    ref = str(ref).strip()
    if ref.isdigit():
        row = conn.execute("SELECT id FROM deals WHERE id = ? OR hubspot_id = ?",
                           (int(ref) if len(ref) < 18 else -1, ref)).fetchone()
        if row:
            return row["id"]
    matches = conn.execute("SELECT id, name FROM deals WHERE name" + LIKE + " ORDER BY updated_at DESC",
                           (_like(ref),)).fetchall()
    exact = [m for m in matches if m["name"].lower() == ref.lower()]
    if len(exact) == 1:
        return exact[0]["id"]
    if len(matches) == 1:
        return matches[0]["id"]
    if not matches:
        raise CRMError("No deal matches %r" % ref)
    raise CRMError("%r matches %d deals, be more specific or use an id: %s" % (
        ref, len(matches), "; ".join("#%d %s" % (m["id"], m["name"]) for m in matches[:10])))


DEAL_SELECT = """
SELECT d.id, d.hubspot_id, d.name, c.name AS company, d.stage, s.category AS stage_category,
       d.amount, d.close_date, d.owner, d.next_step, d.created_at, d.updated_at
FROM deals d
LEFT JOIN companies c ON c.id = d.company_id
JOIN stages s ON s.name = d.stage
"""


# ---------------------------------------------------------------- deals

def search_deals(conn, query=None, stage=None, category=None, owner=None, company=None,
                 close_from=None, close_to=None, min_amount=None, sort="updated", limit=50):
    where, args = [], []
    if query:
        where.append("(d.name" + LIKE + " OR c.name" + LIKE + " OR d.next_step" + LIKE + ")")
        args += [_like(query)] * 3
    if stage:
        where.append("d.stage = ?"); args.append(resolve_stage(conn, stage))
    if category:
        where.append("s.category = ?"); args.append(category)
    if owner:
        where.append("d.owner" + LIKE); args.append(_like(owner))
    if company:
        where.append("c.name" + LIKE); args.append(_like(company))
    if close_from:
        where.append("d.close_date >= ?"); args.append(_norm_date(close_from))
    if close_to:
        where.append("d.close_date <= ?"); args.append(_norm_date(close_to))
    if min_amount is not None:
        where.append("d.amount >= ?"); args.append(_norm_amount(min_amount))
    order = {
        "updated": "d.updated_at DESC, d.id DESC",
        "amount": "COALESCE(d.amount,0) DESC, d.id",
        "close_date": "d.close_date IS NULL, d.close_date ASC, d.id",
        "name": "lower(d.name)",
        "stage": "s.sort_order, COALESCE(d.amount,0) DESC, d.id",
    }.get(sort, "d.updated_at DESC, d.id DESC")
    sql = DEAL_SELECT + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY " + order + " LIMIT ?"
    args.append(max(1, min(int(limit or 50), 500)))
    return _rows(conn.execute(sql, args))


def get_deal(conn, ref):
    deal_id = find_deal(conn, ref)
    deal = dict(conn.execute(DEAL_SELECT + " WHERE d.id = ?", (deal_id,)).fetchone())
    deal["activities"] = _rows(conn.execute(
        "SELECT id, type, body, due_date, done, author, created_at FROM activities "
        "WHERE deal_id = ? ORDER BY created_at DESC, id DESC", (deal_id,)))
    deal["history"] = _rows(conn.execute(
        "SELECT field, old_value, new_value, changed_by, changed_at FROM deal_history "
        "WHERE deal_id = ? ORDER BY id DESC LIMIT 50", (deal_id,)))
    return deal


def _log(conn, deal_id, field, old, new, user):
    conn.execute("INSERT INTO deal_history(deal_id, field, old_value, new_value, changed_by, changed_at) "
                 "VALUES (?,?,?,?,?,?)",
                 (deal_id, field, None if old is None else str(old), None if new is None else str(new), user, _now()))


def create_deal(conn, name, company=None, stage="Prospect", amount=None, close_date=None,
                owner=None, next_step=None, user=None):
    name = (name or "").strip()
    if not name:
        raise CRMError("Deal name is required")
    if not company and " - " in name:
        company = name.split(" - ", 1)[0]
    now = _now()
    deal_id = conn.insert(
        "INSERT INTO deals(name, company_id, stage, amount, close_date, owner, next_step, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (name, get_or_create_company(conn, company), resolve_stage(conn, stage or "Prospect"),
         _norm_amount(amount), _norm_date(close_date), owner or None, next_step or None, now, now))
    _log(conn, deal_id, "created", None, name, user)
    conn.commit()
    return get_deal(conn, deal_id)


def update_deal(conn, ref, user=None, **changes):
    deal_id = find_deal(conn, ref)
    current = dict(conn.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone())
    updates = {}
    for key, value in changes.items():
        if value is None:
            continue
        if isinstance(value, str):
            value = value.strip()
            if value == "":
                if key in ("owner", "next_step", "close_date", "amount", "company"):
                    value = None  # blank = clear the field
                else:
                    continue
        if key == "company":
            key, value = "company_id", (get_or_create_company(conn, value) if value is not None else None)
        elif value is None:
            pass
        elif key == "stage":
            value = resolve_stage(conn, value)
        elif key == "amount":
            value = _norm_amount(value)
        elif key == "close_date":
            value = _norm_date(value)
        elif key not in ("name", "owner", "next_step"):
            raise CRMError("Field %r cannot be updated" % key)
        if current.get(key) != value:
            updates[key] = value
    if not updates:
        return get_deal(conn, deal_id)
    sets = ", ".join("%s = ?" % k for k in updates) + ", updated_at = ?"
    conn.execute("UPDATE deals SET " + sets + " WHERE id = ?", list(updates.values()) + [_now(), deal_id])
    for k, v in updates.items():
        if k in TRACKED_FIELDS:
            _log(conn, deal_id, k, current.get(k), v, user)
    conn.commit()
    return get_deal(conn, deal_id)


def delete_deal(conn, ref):
    deal_id = find_deal(conn, ref)
    name = conn.execute("SELECT name FROM deals WHERE id = ?", (deal_id,)).fetchone()["name"]
    conn.execute("DELETE FROM deals WHERE id = ?", (deal_id,))
    conn.commit()
    return {"deleted": deal_id, "name": name}


# ---------------------------------------------------------------- activities

def add_activity(conn, body, type="note", deal=None, company=None, contact_id=None,
                 due_date=None, author=None):
    if type not in ("note", "call", "meeting", "email", "task"):
        raise CRMError("type must be one of note, call, meeting, email, task")
    if not (body or "").strip():
        raise CRMError("Activity text is required")
    deal_id = find_deal(conn, deal) if deal else None
    company_id = None
    if company:
        rows = conn.execute("SELECT id FROM companies WHERE name" + LIKE, (_like(company),)).fetchall()
        if len(rows) != 1:
            raise CRMError("Company %r matched %d companies" % (company, len(rows)))
        company_id = rows[0]["id"]
    elif deal_id:
        company_id = conn.execute("SELECT company_id FROM deals WHERE id = ?", (deal_id,)).fetchone()["company_id"]
    now = _now()
    act_id = conn.insert(
        "INSERT INTO activities(deal_id, company_id, contact_id, type, body, due_date, author, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (deal_id, company_id, contact_id, type, body.strip(), _norm_date(due_date), author, now))
    if deal_id:
        conn.execute("UPDATE deals SET updated_at = ? WHERE id = ?", (now, deal_id))
    conn.commit()
    return dict(conn.execute("SELECT * FROM activities WHERE id = ?", (act_id,)).fetchone())


def list_tasks(conn, owner=None, include_done=False, due_before=None):
    where = ["a.type = 'task'"]
    args = []
    if not include_done:
        where.append("a.done = 0")
    if owner:
        where.append("(a.author" + LIKE + " OR d.owner" + LIKE + ")"); args += [_like(owner)] * 2
    if due_before:
        where.append("a.due_date <= ?"); args.append(_norm_date(due_before))
    return _rows(conn.execute(
        "SELECT a.id, a.body, a.due_date, a.done, a.author, d.id AS deal_id, d.name AS deal, d.owner "
        "FROM activities a LEFT JOIN deals d ON d.id = a.deal_id WHERE " + " AND ".join(where) +
        " ORDER BY a.due_date IS NULL, a.due_date, a.id", args))


def complete_task(conn, task_id, done=True):
    cur = conn.execute("UPDATE activities SET done = ? WHERE id = ? AND type = 'task'",
                       (1 if done else 0, int(task_id)))
    if cur.rowcount == 0:
        raise CRMError("No task with id %s" % task_id)
    conn.commit()
    return {"task_id": int(task_id), "done": bool(done)}


# ---------------------------------------------------------------- companies & contacts

def list_companies(conn, query=None, limit=100):
    args = []
    sql = ("SELECT c.id, c.name, c.industry, c.website, COUNT(d.id) AS deals, "
           "SUM(CASE WHEN s.category='open' THEN COALESCE(d.amount,0) END) AS open_value, "
           "SUM(CASE WHEN s.category='won' THEN COALESCE(d.amount,0) END) AS won_value "
           "FROM companies c LEFT JOIN deals d ON d.company_id = c.id LEFT JOIN stages s ON s.name = d.stage ")
    if query:
        sql += "WHERE c.name" + LIKE + " "; args.append(_like(query))
    sql += "GROUP BY c.id, c.name, c.industry, c.website ORDER BY lower(c.name) LIMIT ?"
    args.append(int(limit))
    return _rows(conn.execute(sql, args))


def get_company(conn, name):
    name = (name or "").strip()
    if not name:
        raise CRMError("Company name is required")
    rows = conn.execute("SELECT * FROM companies WHERE name" + LIKE + " ORDER BY lower(name)", (_like(name),)).fetchall()
    exact = [r for r in rows if r["name"].lower() == name.lower()]
    rows = exact or rows
    if len(rows) != 1:
        raise CRMError("%r matched %d companies: %s" % (name, len(rows), ", ".join(r["name"] for r in rows[:15])))
    company = dict(rows[0])
    company["deals"] = _rows(conn.execute(DEAL_SELECT + " WHERE d.company_id = ? ORDER BY d.close_date DESC, d.id",
                                          (company["id"],)))
    company["contacts"] = _rows(conn.execute("SELECT * FROM contacts WHERE company_id = ? ORDER BY lower(name)",
                                             (company["id"],)))
    company["activities"] = _rows(conn.execute(
        "SELECT * FROM activities WHERE company_id = ? ORDER BY created_at DESC, id DESC LIMIT 30", (company["id"],)))
    return company


def update_company(conn, name, new_name=None, website=None, industry=None, notes=None):
    company = get_company(conn, name)
    fields = {k: v for k, v in (("name", new_name), ("website", website), ("industry", industry),
                                 ("notes", notes)) if v is not None}
    if fields:
        conn.execute("UPDATE companies SET " + ", ".join("%s = ?" % k for k in fields) + " WHERE id = ?",
                     list(fields.values()) + [company["id"]])
        conn.commit()
    return get_company(conn, new_name or company["name"])


def add_contact(conn, name, company=None, email=None, phone=None, title=None, notes=None):
    if not (name or "").strip():
        raise CRMError("Contact name is required")
    cid = conn.insert(
        "INSERT INTO contacts(company_id, name, email, phone, title, notes, created_at) VALUES (?,?,?,?,?,?,?)",
        (get_or_create_company(conn, company), name.strip(), email, phone, title, notes, _now()))
    conn.commit()
    return dict(conn.execute("SELECT * FROM contacts WHERE id = ?", (cid,)).fetchone())


def search_contacts(conn, query):
    return _rows(conn.execute(
        "SELECT ct.*, c.name AS company FROM contacts ct LEFT JOIN companies c ON c.id = ct.company_id "
        "WHERE ct.name" + LIKE + " OR ct.email" + LIKE + " OR c.name" + LIKE + " ORDER BY lower(ct.name)",
        [_like(query)] * 3))


# ---------------------------------------------------------------- reporting

def pipeline_summary(conn, owner=None, close_from=None, close_to=None):
    where, args = [], []
    if owner:
        where.append("d.owner" + LIKE); args.append(_like(owner))
    if close_from:
        where.append("d.close_date >= ?"); args.append(_norm_date(close_from))
    if close_to:
        where.append("d.close_date <= ?"); args.append(_norm_date(close_to))
    w = (" AND " + " AND ".join(where)) if where else ""
    by_stage = _rows(conn.execute(
        "SELECT s.name AS stage, s.category, COUNT(d.id) AS deals, COALESCE(SUM(d.amount),0) AS value, "
        "COALESCE(SUM(d.amount * s.probability / 100.0),0) AS weighted_value "
        "FROM stages s LEFT JOIN deals d ON d.stage = s.name" + w +
        " GROUP BY s.name, s.category, s.sort_order ORDER BY s.sort_order, s.name", args))
    by_owner = _rows(conn.execute(
        "SELECT COALESCE(d.owner,'(unassigned)') AS owner, "
        "SUM(CASE WHEN s.category='open' THEN 1 ELSE 0 END) AS open_deals, "
        "COALESCE(SUM(CASE WHEN s.category='open' THEN d.amount END),0) AS open_value, "
        "SUM(CASE WHEN s.category='won' THEN 1 ELSE 0 END) AS won_deals, "
        "COALESCE(SUM(CASE WHEN s.category='won' THEN d.amount END),0) AS won_value, "
        "SUM(CASE WHEN s.category='lost' THEN 1 ELSE 0 END) AS lost_deals "
        "FROM deals d JOIN stages s ON s.name = d.stage WHERE 1=1" + w +
        " GROUP BY COALESCE(d.owner,'(unassigned)') ORDER BY open_value DESC", args))
    overdue = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(d.amount),0) AS v FROM deals d JOIN stages s ON s.name = d.stage "
        "WHERE s.category = 'open' AND d.close_date < ?" + w, [_today()] + args).fetchone()
    won = sum(r["deals"] for r in by_stage if r["category"] == "won")
    lost = sum(r["deals"] for r in by_stage if r["category"] == "lost")
    return {
        "open_pipeline_value": float(sum(r["value"] for r in by_stage if r["category"] == "open")),
        "weighted_pipeline_value": round(float(sum(r["weighted_value"] for r in by_stage if r["category"] == "open")), 2),
        "past_close_date_deals": int(overdue["n"]),
        "past_close_date_value": float(overdue["v"]),
        "won_value": float(sum(r["value"] for r in by_stage if r["category"] == "won")),
        "win_rate_pct": round(100.0 * won / (won + lost), 1) if (won + lost) else None,
        "by_stage": by_stage,
        "by_owner": by_owner,
        "notes": "Win rate = won / (won + lost) by count. Weighted value uses stage probabilities in the stages table.",
    }


def stale_deals(conn, days=30, owner=None):
    """Open deals not touched in N days, or whose close date has passed."""
    cutoff = (_dt.datetime.utcnow() - _dt.timedelta(days=int(days))).strftime("%Y-%m-%d %H:%M:%S")
    args = [cutoff, _today()]
    sql = DEAL_SELECT + " WHERE s.category = 'open' AND (d.updated_at < ? OR d.close_date < ?)"
    if owner:
        sql += " AND d.owner" + LIKE; args.append(_like(owner))
    return _rows(conn.execute(sql + " ORDER BY COALESCE(d.amount,0) DESC, d.id", args))


def read_only_query(sql, path=None):
    """Run an ad-hoc SELECT on a read-only connection (for questions the tools don't cover).

    On Postgres, set READONLY_DATABASE_URL to a login with SELECT-only grants for defence in
    depth; otherwise DATABASE_URL is used inside a READ ONLY transaction with a timeout.
    """
    s = sql.strip().rstrip(";")
    if not s.lower().startswith(("select", "with")) or ";" in s:
        raise CRMError("Only a single SELECT (or WITH ... SELECT) statement is allowed")
    url = os.environ.get("READONLY_DATABASE_URL") or database_url()
    if url and path is None:
        import psycopg
        conn = psycopg.connect(url, autocommit=False)
        try:
            conn.read_only = True
            conn.execute("SET LOCAL statement_timeout = '5s'")
            cur = conn.execute(s.encode())  # bytes: no placeholder processing
            rows = cur.fetchmany(500)
            cols = [c.name for c in cur.description or []]
            return {"columns": cols, "rows": [[_jsonable(v) for v in r] for r in rows], "truncated": len(rows) == 500}
        except psycopg.Error as e:
            raise CRMError("SQL error: %s" % str(e).strip())
        finally:
            conn.rollback()
            conn.close()
    conn = sqlite3.connect("file:%s?mode=ro" % (path or DB_PATH), uri=True)
    try:
        cur = conn.execute(s)
        rows = cur.fetchmany(500)
        return {"columns": [c[0] for c in cur.description or []], "rows": [list(r) for r in rows],
                "truncated": len(rows) == 500}
    except sqlite3.Error as e:
        raise CRMError("SQL error: %s" % e)
    finally:
        conn.close()


def _jsonable(v):
    import decimal
    return float(v) if isinstance(v, decimal.Decimal) else v
