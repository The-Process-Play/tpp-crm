"""One-off copy of the local SQLite CRM into Postgres (e.g. Azure Database for PostgreSQL).

    DATABASE_URL='postgresql://user:password@host:5432/crm?sslmode=require' \\
        .venv/bin/python -m crm.migrate_sqlite_to_postgres [path/to/crm.db]

Creates the schema if needed, copies every table keeping the same ids (so deal numbers
don't change), then resets the id counters. Refuses to run if the target already holds
deals, so it can't double-load or overwrite live data. Everything runs in one
transaction: if any step fails, nothing is written.
"""
import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from crm import db  # noqa: E402

# Parent tables before children, so foreign keys are satisfied.
TABLES = ["stages", "companies", "contacts", "deals", "activities", "deal_history"]
ID_TABLES = ["companies", "contacts", "deals", "activities", "deal_history"]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sqlite_path", nargs="?", default=db.DB_PATH)
    args = ap.parse_args(argv)
    if not db.database_url():
        sys.exit("Set DATABASE_URL to the target Postgres database first.")
    if not os.path.exists(args.sqlite_path):
        sys.exit("SQLite file not found: %s" % args.sqlite_path)

    src = sqlite3.connect("file:%s?mode=ro" % args.sqlite_path, uri=True)
    src.row_factory = sqlite3.Row
    dst = db.connect()
    db.init_db(dst)
    existing = dst.execute("SELECT COUNT(*) AS n FROM deals").fetchone()["n"]
    if existing:
        sys.exit("Target already has %d deals. Migration is for an empty database only." % existing)

    counts = {}
    try:
        for table in TABLES:
            rows = src.execute("SELECT * FROM %s" % table).fetchall()
            if not rows:
                counts[table] = 0
                continue
            cols = rows[0].keys()
            sql = "INSERT INTO %s (%s) VALUES (%s)" % (table, ", ".join(cols), ", ".join("?" * len(cols)))
            if table == "stages":
                sql += " ON CONFLICT(name) DO UPDATE SET category=excluded.category, " \
                       "sort_order=excluded.sort_order, probability=excluded.probability"
            for r in rows:
                dst.execute(sql, tuple(r))
            counts[table] = len(rows)
        for table in ID_TABLES:
            dst.execute("SELECT setval(pg_get_serial_sequence('%s', 'id'), "
                        "COALESCE((SELECT MAX(id) FROM %s), 0) + 1, false)" % (table, table))
        dst.commit()
    except Exception:
        dst.rollback()
        raise
    finally:
        src.close()
        dst.close()

    print("Copied to Postgres:")
    for t in TABLES:
        print("  %-13s %d" % (t, counts[t]))


if __name__ == "__main__":
    main()
