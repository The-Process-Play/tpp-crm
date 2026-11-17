# TPP CRM: notes for Claude

This folder is The Process Play's CRM. Use the `tpp-crm` MCP tools to read and change it, not raw edits to `data/crm.db`.

- Amounts are GBP. Show them as £ with thousands separators.
- Deal names follow "Company - Description". Keep that pattern when creating deals.
- Before `delete_deal`, confirm with the user. Prefer moving the deal to "Closed lost" or "Ignore" instead.
- If a deal reference matches several deals, show the matches and ask which one.
- "This quarter" means the calendar quarter unless the user says otherwise. Use explicit dates in tool calls.
- Most open deals imported from HubSpot have close dates in the past. When reporting pipeline, mention how much of it is past its close date.
- Code changes: all business rules live in `crm/db.py`, and the same SQL must run on SQLite and Postgres (write `?` placeholders and `LIKE`; the Connection wrapper adapts them). Keep the SQLite path standard-library only so it runs on macOS system Python 3.9. Run `python3 -m unittest discover -s tests` and, where possible, the Postgres run too (see README).
- Never commit CRM data (`data/`, `*.db`, exports). The repo is code only.
- Brand: use the CSS tokens in `crm/static/index.html` (`--tpp-1/2/3`, IBM Plex Sans). Never retype or recolour the logo; use `crm/static/tpp-logo.svg` (white in dark mode is the only approved change).
