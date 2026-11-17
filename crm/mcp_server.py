"""TPP CRM connector for Claude (Model Context Protocol, stdio transport).

Dependency-free: implements the small slice of MCP needed for tools
(initialize, tools/list, tools/call, ping) over newline-delimited JSON-RPC,
so it runs on the system Python with no installs.

Run by Claude Code / Claude Desktop, not by hand. See README.md for setup.
Set TPP_CRM_USER to your name so changes are attributed in the audit trail.
"""
import json
import os
import sys
import traceback

# Allow running as a script (python crm/mcp_server.py) as well as a module.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from crm import db  # noqa: E402

SERVER_INFO = {"name": "tpp-crm", "version": "1.0.0"}
SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
USER = os.environ.get("TPP_CRM_USER") or None

INSTRUCTIONS = """TPP CRM: The Process Play's deal pipeline (imported from HubSpot on 2026-11-17).
- Amounts are GBP. Stages (in order): %s.
- Stage categories: open (live pipeline), won, lost, parked (newsletter/ignore/hold).
- Deals can be referenced by id, HubSpot Record ID, or a unique part of the name.
- Before changing or deleting anything, confirm the exact deal with the user if the match is ambiguous.
- Use run_sql only for questions the other tools cannot answer.""" % ", ".join(db.STAGE_NAMES)


def _s(desc, **extra):
    d = {"type": "string", "description": desc}
    d.update(extra)
    return d


STAGE_PROP = _s("Pipeline stage", enum=db.STAGE_NAMES)
DEAL_REF = _s("Deal id, HubSpot Record ID, or unique part of the deal name")

TOOLS = [
    {
        "name": "search_deals",
        "description": "Search/filter deals. All filters optional and combined with AND. "
                       "Returns up to `limit` deals with company, stage, amount, close date and owner.",
        "inputSchema": {"type": "object", "properties": {
            "query": _s("Text to match in deal name, company or next step"),
            "stage": STAGE_PROP,
            "category": _s("Stage category", enum=["open", "won", "lost", "parked"]),
            "owner": _s("Deal owner (partial match)"),
            "company": _s("Company name (partial match)"),
            "close_from": _s("Close date on/after, YYYY-MM-DD"),
            "close_to": _s("Close date on/before, YYYY-MM-DD"),
            "min_amount": {"type": "number", "description": "Minimum amount (GBP)"},
            "sort": _s("Sort order", enum=["updated", "amount", "close_date", "name", "stage"]),
            "limit": {"type": "integer", "description": "Max results (default 50, max 500)"},
        }},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "get_deal",
        "description": "Full detail for one deal: fields, notes/activities/tasks and change history.",
        "inputSchema": {"type": "object", "properties": {"deal": DEAL_REF}, "required": ["deal"]},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "create_deal",
        "description": "Create a new deal. If company is omitted and the name is 'Company - Description', "
                       "the company is taken from the name. New companies are created automatically.",
        "inputSchema": {"type": "object", "properties": {
            "name": _s("Deal name, ideally 'Company - Description'"),
            "company": _s("Company name"),
            "stage": STAGE_PROP,
            "amount": {"type": "number", "description": "Value in GBP"},
            "close_date": _s("Expected close date, YYYY-MM-DD"),
            "owner": _s("Deal owner's full name"),
            "next_step": _s("Next action on the deal"),
        }, "required": ["name"]},
    },
    {
        "name": "update_deal",
        "description": "Change one or more fields on a deal (stage, amount, close date, owner, company, name, "
                       "next step). Only supplied fields change; every change is logged in the deal history.",
        "inputSchema": {"type": "object", "properties": {
            "deal": DEAL_REF,
            "name": _s("New deal name"),
            "company": _s("Company name"),
            "stage": STAGE_PROP,
            "amount": {"type": "number", "description": "Value in GBP"},
            "close_date": _s("YYYY-MM-DD"),
            "owner": _s("Deal owner's full name"),
            "next_step": _s("Next action on the deal"),
        }, "required": ["deal"]},
        "annotations": {"idempotentHint": True},
    },
    {
        "name": "delete_deal",
        "description": "Permanently delete a deal and its notes. Cannot be undone; prefer moving it to "
                       "'Closed lost' or 'Ignore'. Only use when the user explicitly asks to delete.",
        "inputSchema": {"type": "object", "properties": {"deal": DEAL_REF}, "required": ["deal"]},
        "annotations": {"destructiveHint": True},
    },
    {
        "name": "add_activity",
        "description": "Log a note, call, meeting or email against a deal or company, or create a task "
                       "(type='task' with optional due_date).",
        "inputSchema": {"type": "object", "properties": {
            "body": _s("What happened / what needs doing"),
            "type": _s("Activity type", enum=["note", "call", "meeting", "email", "task"]),
            "deal": DEAL_REF,
            "company": _s("Company name (if not tied to a deal)"),
            "due_date": _s("For tasks: YYYY-MM-DD"),
        }, "required": ["body"]},
    },
    {
        "name": "list_tasks",
        "description": "List open tasks (optionally for one person, or due on/before a date).",
        "inputSchema": {"type": "object", "properties": {
            "owner": _s("Task author or deal owner (partial match)"),
            "due_before": _s("YYYY-MM-DD"),
            "include_done": {"type": "boolean"},
        }},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "complete_task",
        "description": "Mark a task done (or not done).",
        "inputSchema": {"type": "object", "properties": {
            "task_id": {"type": "integer"}, "done": {"type": "boolean", "description": "Default true"},
        }, "required": ["task_id"]},
        "annotations": {"idempotentHint": True},
    },
    {
        "name": "pipeline_summary",
        "description": "Pipeline report: open, weighted and won value, win rate, and breakdowns by stage and "
                       "by owner. Optional owner and close-date window filters.",
        "inputSchema": {"type": "object", "properties": {
            "owner": _s("Deal owner (partial match)"),
            "close_from": _s("YYYY-MM-DD"), "close_to": _s("YYYY-MM-DD"),
        }},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "stale_deals",
        "description": "Open deals that need attention: not updated in `days` days, or past their close date.",
        "inputSchema": {"type": "object", "properties": {
            "days": {"type": "integer", "description": "Default 30"}, "owner": _s("Deal owner (partial match)"),
        }},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "list_companies",
        "description": "List companies with deal count, open pipeline value and won value.",
        "inputSchema": {"type": "object", "properties": {
            "query": _s("Name filter (partial match)"), "limit": {"type": "integer"},
        }},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "get_company",
        "description": "One company with all its deals, contacts and recent activity.",
        "inputSchema": {"type": "object", "properties": {"name": _s("Company name")}, "required": ["name"]},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "update_company",
        "description": "Rename a company or set its website, industry or notes.",
        "inputSchema": {"type": "object", "properties": {
            "name": _s("Current company name"), "new_name": _s("New name"),
            "website": _s("Website"), "industry": _s("Industry"), "notes": _s("Notes"),
        }, "required": ["name"]},
    },
    {
        "name": "add_contact",
        "description": "Add a contact person, optionally linked to a company.",
        "inputSchema": {"type": "object", "properties": {
            "name": _s("Full name"), "company": _s("Company name"), "email": _s("Email"),
            "phone": _s("Phone"), "title": _s("Job title"), "notes": _s("Notes"),
        }, "required": ["name"]},
    },
    {
        "name": "search_contacts",
        "description": "Find contacts by name, email or company.",
        "inputSchema": {"type": "object", "properties": {"query": _s("Search text")}, "required": ["query"]},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "run_sql",
        "description": "Read-only SQL (single SELECT) for questions other tools can't answer. Tables: "
                       "deals(id, hubspot_id, name, company_id, stage, amount, close_date, owner, next_step, "
                       "created_at, updated_at), companies(id, name, website, industry, notes), "
                       "contacts(id, company_id, name, email, phone, title, notes), "
                       "activities(id, deal_id, company_id, contact_id, type, body, due_date, done, author, created_at), "
                       "deal_history(deal_id, field, old_value, new_value, changed_by, changed_at), "
                       "stages(name, category, sort_order, probability).",
        "inputSchema": {"type": "object", "properties": {"sql": _s("SELECT statement")}, "required": ["sql"]},
        "annotations": {"readOnlyHint": True},
    },
]


def call_tool(name, a):
    conn = db.connect()
    try:
        if name == "search_deals":
            return db.search_deals(conn, **a)
        if name == "get_deal":
            return db.get_deal(conn, a["deal"])
        if name == "create_deal":
            return db.create_deal(conn, user=USER, **a)
        if name == "update_deal":
            ref = a.pop("deal")
            return db.update_deal(conn, ref, user=USER, **a)
        if name == "delete_deal":
            return db.delete_deal(conn, a["deal"])
        if name == "add_activity":
            return db.add_activity(conn, author=USER, **a)
        if name == "list_tasks":
            return db.list_tasks(conn, **a)
        if name == "complete_task":
            return db.complete_task(conn, a["task_id"], a.get("done", True))
        if name == "pipeline_summary":
            return db.pipeline_summary(conn, **a)
        if name == "stale_deals":
            return db.stale_deals(conn, **a)
        if name == "list_companies":
            return db.list_companies(conn, **a)
        if name == "get_company":
            return db.get_company(conn, a["name"])
        if name == "update_company":
            return db.update_company(conn, **a)
        if name == "add_contact":
            return db.add_contact(conn, **a)
        if name == "search_contacts":
            return db.search_contacts(conn, a["query"])
        if name == "run_sql":
            return db.read_only_query(a["sql"])
        raise db.CRMError("Unknown tool %r" % name)
    finally:
        conn.close()


def handle(msg):
    method = msg.get("method")
    params = msg.get("params") or {}
    if method == "initialize":
        requested = params.get("protocolVersion")
        return {
            "protocolVersion": requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS,
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        name = params.get("name")
        args = dict(params.get("arguments") or {})
        try:
            result = call_tool(name, args)
            text = json.dumps(result, default=str, indent=1)
            return {"content": [{"type": "text", "text": text}], "isError": False}
        except (db.CRMError, TypeError, KeyError) as e:
            return {"content": [{"type": "text", "text": "Error: %s" % e}], "isError": True}
        except Exception as e:  # unexpected: report to Claude, log detail to stderr
            traceback.print_exc(file=sys.stderr)
            return {"content": [{"type": "text", "text": "Internal error: %s" % e}], "isError": True}
    raise LookupError(method)


def main():
    conn = db.connect()
    db.init_db(conn)  # make sure schema exists even before an import
    conn.close()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if "id" not in msg:  # notification (e.g. notifications/initialized): no reply
            continue
        reply = {"jsonrpc": "2.0", "id": msg["id"]}
        try:
            reply["result"] = handle(msg)
        except LookupError:
            reply["error"] = {"code": -32601, "message": "Method not found: %s" % msg.get("method")}
        except Exception as e:
            reply["error"] = {"code": -32603, "message": str(e)}
        sys.stdout.write(json.dumps(reply, default=str) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
