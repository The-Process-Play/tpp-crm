# TPP CRM

A lightweight CRM for The Process Play: deals, companies, contacts, notes and tasks, with a pipeline
board in the browser and a connector so you can run it by asking Claude in plain English.

It was seeded from a HubSpot "All deals" export (Nov 2026). **No CRM data is stored in this
repository.** The database lives either in a local file (`data/crm.db`, git-ignored) or in Azure
Postgres once hosted. A fresh clone starts with an empty pipeline.

## Two ways in

1. **Claude.** Ask things like "what's in Negotiation?", "move the Acme rollout to won at £120k",
   or "log a call on that deal and add a task for Thursday". Claude uses the `tpp-crm` connector
   (an MCP server).
2. **Web app.** A pipeline board with drag and drop, a deal list, tasks, company pages and CSV export.

## Layout

| Path | What it is |
|---|---|
| `crm/db.py` | All business rules: schema, stages, search, updates, reports. Runs on SQLite or Postgres |
| `crm/web.py`, `crm/static/index.html` | Web app and its JSON API |
| `crm/mcp_server.py` | Claude connector (16 tools), dependency-free MCP over stdio |
| `crm/import_hubspot.py` | Imports HubSpot deal exports (.xls or .csv) |
| `crm/migrate_sqlite_to_postgres.py` | One-off copy from the local file to Postgres |
| `tests/` | Tests, run against SQLite and Postgres |
| `Dockerfile`, `.github/workflows/` | Container, CI, and the Azure deploy workflow |
| `docs/AZURE.md` | Step-by-step Azure hosting guide |
| `.mcp.json` | Registers the connector for Claude Code in this folder |

## Running locally

Needs only the Python that ships with macOS (3.9+). Postgres support and `.xls` import need two
packages:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Start the web app, then open http://127.0.0.1:8765:

```bash
python3 crm/web.py
```

Load a HubSpot export. This only adds deals whose Record ID isn't already present, so local edits are
kept; `--update` overwrites stage, amount, close date and owner from the file:

```bash
.venv/bin/python -m crm.import_hubspot ~/Downloads/hubspot-export.xls
```

Set `DATABASE_URL=postgresql://...` to run any of these against Postgres instead of the local file.

### Web app features

- **Pipeline board**: drag a card between columns to change its stage. While dragging, a bar appears at
  the bottom for Closed lost, Hold, Newsletter and Ignore. Each move can be undone.
- **Overdue flags**: open deals past their close date are marked, with an "Overdue only" filter.
- **All deals**: a sortable list. **Deal panel**: edit, log notes, calls, meetings, emails and tasks, and see history.
- **Company panel**: deals, open and won value, and contacts. **Tasks**: open tasks with tick-to-complete.
- **Export CSV** of the deals currently shown.

Drag and drop needs a mouse or trackpad. On a phone, change the stage in the deal panel.

## Using it through Claude

**Claude Code:** open a session in this folder and approve the `tpp-crm` server. Set your name for the
audit trail with `export TPP_CRM_USER="Your Name"` before starting Claude Code.

**Claude Desktop (Chat tab):** add this to `~/Library/Application Support/Claude/claude_desktop_config.json`
and restart Claude:

```json
{
  "mcpServers": {
    "tpp-crm": {
      "command": "/usr/bin/python3",
      "args": ["/path/to/tpp-crm/crm/mcp_server.py"],
      "env": { "TPP_CRM_USER": "Your Name" }
    }
  }
}
```

| Tool | Purpose |
|---|---|
| `search_deals`, `get_deal` | Find and inspect deals (by id, HubSpot ID or name) |
| `create_deal`, `update_deal`, `delete_deal` | Change the pipeline; every field change is logged |
| `add_activity`, `list_tasks`, `complete_task` | Notes, calls, meetings, emails and tasks |
| `pipeline_summary`, `stale_deals` | Open, weighted, won and overdue value, win rate, deals needing attention |
| `list_companies`, `get_company`, `update_company` | Company view and cleanup |
| `add_contact`, `search_contacts` | People |
| `run_sql` | Read-only SQL for anything else |

For Claude against the hosted version, see section 6 of [docs/AZURE.md](docs/AZURE.md).

## Hosting on Azure

See [docs/AZURE.md](docs/AZURE.md). In short: a container on App Service behind Microsoft 365 sign-in,
with Azure Database for PostgreSQL in UK South, deployed from GitHub Actions.

| Setting | Purpose |
|---|---|
| `DATABASE_URL` | Postgres connection string. If unset, the local SQLite file is used |
| `READONLY_DATABASE_URL` | Optional SELECT-only login for Claude's `run_sql` tool |
| `TRUST_EASY_AUTH=1` | Take the user from App Service sign-in, and refuse requests without one |
| `HOST`, `PORT` | Where the web app listens (container defaults: `0.0.0.0:8000`) |
| `TPP_CRM_USER` | Name recorded on changes when running locally |

## Development

```bash
python3 -m unittest discover -s tests -v
```

To include Postgres, point `TEST_DATABASE_URL` at a **throwaway** database. The tests drop and recreate the tables:

```bash
TEST_DATABASE_URL=postgresql://localhost/crm_test .venv/bin/python -m unittest discover -s tests -v
```

CI runs both on every push and pull request, on Python 3.9 and 3.12, and checks the container builds.
Keep `crm/db.py` and the SQLite path standard-library only, so the connector runs on a bare Mac.

## Pipeline stages

Kept as in HubSpot. Probabilities feed the weighted pipeline and live in the `stages` table.

| Stage | Category | Probability |
|---|---|---|
| Prospect | open | 10% |
| To Chase | open | 15% |
| Awaiting More Information | open | 25% |
| In Discussion | open | 35% |
| Workshop/Strategy | open | 50% |
| Negotiation | open | 75% |
| Close Won | won | 100% |
| Closed lost | lost | – |
| Hold / Add to monthly newsletter list / Ignore | parked | – |

The probabilities are placeholders, not figures from HubSpot. Adjust them to your own conversion rates.

## Known limitations

- **Companies were inferred** from deal names ("Company - Description"). Deals without that pattern
  became their own company, so a handful need tidying.
- **No contacts were in the HubSpot export**, so contacts start empty.
- **Anyone who can sign in can edit and delete.** Roles aren't built yet.
- **Local SQLite is single-machine.** Don't share `crm.db` over OneDrive or Dropbox, because the file can corrupt.
