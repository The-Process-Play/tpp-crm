"""Import a HubSpot "All deals" export into the CRM database.

Usage:
    .venv/bin/python -m crm.import_hubspot path/to/export.xls [--update]

Supports .xls (needs `xlrd`, installed in .venv) and .csv (stdlib only).
Writes to the local SQLite file, or to Postgres when DATABASE_URL is set.
Expected columns: Record ID, Deal Name, Deal Stage, Close Date, Deal owner, Amount.

By default only deals whose Record ID is not already in the CRM are added, so
changes made in the CRM are never overwritten. Pass --update to also overwrite
stage/amount/close date/owner on existing deals from the file.
"""
import argparse
import csv
import datetime as dt
import os
import sys

from . import db

DEACTIVATED = " (Deactivated User)"


def read_rows(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        with open(path, newline="", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f)), None
    if ext in (".xls", ".xlsx"):
        try:
            import xlrd
        except ImportError:
            sys.exit("Reading .xls needs xlrd: .venv/bin/pip install xlrd==2.0.1 (or export as CSV)")
        book = xlrd.open_workbook(path)
        sheet = book.sheet_by_index(0)
        header = [str(h).strip() for h in sheet.row_values(0)]
        return [dict(zip(header, sheet.row_values(r))) for r in range(1, sheet.nrows)], book.datemode
    sys.exit("Unsupported file type: %s" % ext)


def parse_date(value, datemode):
    if value in ("", None):
        return None
    if isinstance(value, float) and datemode is not None:
        import xlrd
        return xlrd.xldate_as_datetime(value, datemode).date().isoformat()
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y", "%d/%m/%Y %H:%M"):
        try:
            return dt.datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def parse_id(value):
    if isinstance(value, float):
        return str(int(value))
    return str(value).strip()


def company_from_deal_name(name):
    """HubSpot deal names mostly follow 'Company - Description'.
    Names without that separator are treated as the company name itself;
    a handful will need tidying (list_companies / update_company)."""
    return name.split(" - ", 1)[0].strip() if " - " in name else name.strip()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path")
    ap.add_argument("--update", action="store_true", help="overwrite fields on deals that already exist")
    args = ap.parse_args(argv)

    rows, datemode = read_rows(args.path)
    os.makedirs(os.path.dirname(db.DB_PATH), exist_ok=True)
    conn = db.connect()
    db.init_db(conn)

    added = updated = skipped = 0
    problems = []
    for i, r in enumerate(rows, start=2):
        hid = parse_id(r.get("Record ID", ""))
        name = str(r.get("Deal Name", "")).strip()
        if not hid or not name:
            continue
        try:
            stage = db.resolve_stage(conn, str(r.get("Deal Stage", "")).strip() or "Prospect")
        except db.CRMError:
            stage = str(r.get("Deal Stage")).strip()  # unknown stage: add it as an 'open' stage
            conn.execute("INSERT INTO stages(name, category, sort_order) VALUES (?, 'open', 90) "
                         "ON CONFLICT(name) DO NOTHING", (stage,))
            problems.append("row %d: unknown stage %r added as an open stage" % (i, stage))
        owner = str(r.get("Deal owner", "")).strip().replace(DEACTIVATED, "") or None
        amount = r.get("Amount")
        amount = float(str(amount).replace(",", "")) if str(amount).strip() not in ("", "None") else None
        close = parse_date(r.get("Close Date"), datemode)

        existing = conn.execute("SELECT id FROM deals WHERE hubspot_id = ?", (hid,)).fetchone()
        if existing and not args.update:
            skipped += 1
            continue
        if existing:
            conn.execute("UPDATE deals SET stage=?, amount=?, close_date=?, owner=?, updated_at=? "
                         "WHERE id=?", (stage, amount, close, owner, db._now(), existing["id"]))
            updated += 1
        else:
            company_id = db.get_or_create_company(conn, company_from_deal_name(name))
            # created/updated_at use the close date so "stale deal" checks are meaningful on day one
            stamp = (close + " 00:00:00") if close and close <= dt.date.today().isoformat() else db._now()
            conn.execute(
                "INSERT INTO deals(hubspot_id, name, company_id, stage, amount, close_date, owner, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?, ?, ?)",
                (hid, name, company_id, stage, amount, close, owner, stamp, stamp))
            added += 1
    conn.commit()
    conn.close()

    print("Imported from %s" % os.path.basename(args.path))
    print("  added: %d   updated: %d   skipped (already present): %d" % (added, updated, skipped))
    for p in problems:
        print("  note: " + p)
    print("  database: %s" % ("Postgres (DATABASE_URL)" if db.database_url() else db.DB_PATH))


if __name__ == "__main__":
    main()
