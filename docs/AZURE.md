# Hosting TPP CRM on Azure

This sets up the CRM as a web app for the team, behind Microsoft 365 sign-in, with its data in
Azure's managed Postgres in the UK. Allow about half a day for someone comfortable in the Azure portal.

> **Status:** the code, container and GitHub workflows are ready and tested locally (tests pass on
> SQLite and Postgres, and the migration has been rehearsed with the real data). The Azure steps below
> have **not** been run yet, because they need your Azure subscription. Command names and portal labels
> can change, so check them against the current Azure docs as you go.

## What gets created

| Resource | Suggested name | Notes |
|---|---|---|
| Resource group | `rg-tpp-crm` | Region **UK South** for everything |
| Azure Database for PostgreSQL – Flexible Server | `psql-tpp-crm` | Burstable tier is plenty. Database `crm`. Backups on (default 7 days; consider 35) |
| Azure Container Registry | `acrtppcrm` (globally unique, letters/numbers) | Basic tier |
| App Service plan (Linux) | `asp-tpp-crm` | Basic B1 is enough to start |
| Web App for Containers | `tpp-crm` (becomes `tpp-crm.azurewebsites.net`) | Runs the Docker image |
| Entra ID app registration | `TPP CRM` | Created for you by App Service Authentication |
| Entra ID app registration | `TPP CRM GitHub deploy` | Lets GitHub Actions deploy without stored passwords |

Check pricing for these tiers in the Azure pricing calculator before creating them.

## 1. Database

1. Create the Flexible Server in UK South. Choose **PostgreSQL authentication**, an admin user, and a strong
   password, and store it in your password manager.
2. Networking: start with **Public access** and tick **Allow public access from any Azure service**. Add
   your office or home IP only while you run the migration, then remove it. (A private network is more
   secure, but it adds a virtual network and is worth doing later.)
3. Create a database called `crm`.
4. Create an app login with only what it needs. Connect with any Postgres client as the admin user and run:

   ```sql
   CREATE ROLE crm_app LOGIN PASSWORD '<strong password>';
   GRANT CONNECT ON DATABASE crm TO crm_app;
   GRANT USAGE, CREATE ON SCHEMA public TO crm_app;

   -- Optional, for Claude's read-only SQL tool:
   CREATE ROLE crm_readonly LOGIN PASSWORD '<another strong password>';
   GRANT CONNECT ON DATABASE crm TO crm_readonly;
   GRANT USAGE ON SCHEMA public TO crm_readonly;
   GRANT crm_app TO <your admin user>;  -- needed on Azure to set default privileges for crm_app
   ALTER DEFAULT PRIVILEGES FOR ROLE crm_app IN SCHEMA public GRANT SELECT ON TABLES TO crm_readonly;
   ```

The connection string looks like:
`postgresql://crm_app:<password>@psql-tpp-crm.postgres.database.azure.com:5432/crm?sslmode=require`

## 2. Copy the data across (one-off)

From the project folder on the Mac that has `data/crm.db`:

```bash
.venv/bin/pip install -r requirements.txt
```

```bash
DATABASE_URL='postgresql://crm_app:<password>@psql-tpp-crm.postgres.database.azure.com:5432/crm?sslmode=require' .venv/bin/python -m crm.migrate_sqlite_to_postgres data/crm.db
```

It creates the tables, copies everything with the same deal numbers, and refuses to run twice. After
this, **stop using the local copy**: anything changed on the Mac won't reach Azure.

## 3. Container registry and web app

1. Create the Container Registry (Basic).
2. Create a Web App: Publish **Container**, OS **Linux**, region UK South, the B1 plan. For the first
   deploy you can pick the quickstart image; GitHub Actions replaces it.
3. In the Web App:
   - **Identity**: turn on the system-assigned managed identity. In the Container Registry, under
     **Access control (IAM)**, give that identity the **AcrPull** role.
   - **Deployment Center**: set the registry to your ACR and authentication to **Managed identity**.
   - **Environment variables** (app settings):

     | Name | Value |
     |---|---|
     | `DATABASE_URL` | the `crm_app` connection string. Better: store it in Key Vault and use a Key Vault reference |
     | `READONLY_DATABASE_URL` | the `crm_readonly` connection string (optional) |
     | `TRUST_EASY_AUTH` | `1` |
     | `WEBSITES_PORT` | `8000` |

   - **Configuration → General**: HTTPS only **On**, minimum TLS **1.2**, FTP **Disabled**.
   - **Health check**: path `/healthz`.

## 4. Microsoft 365 sign-in

1. Web App → **Authentication** → **Add identity provider** → **Microsoft**.
2. Create a new app registration, **Current tenant – single tenant** (only your organisation).
3. Restrict access: **Require authentication**. Unauthenticated requests: **HTTP 302 redirect** to Microsoft.
4. Exclude `/healthz` from authentication so health checks work. In the authentication settings
   (auth settings v2 / "File-based configuration"), add it to `globalValidation.excludedPaths`.
5. Optional, to limit access to named people: in **Entra ID → Enterprise applications → TPP CRM →
   Properties**, set **Assignment required** to **Yes**, then add users or a group under **Users and groups**.

The app reads the signed-in person's email from the header App Service adds
(`X-MS-CLIENT-PRINCIPAL-NAME`) and records it on every change. It only trusts that header when
`TRUST_EASY_AUTH=1`, and refuses requests without it.

## 5. GitHub deployment

1. In Entra ID, create an app registration `TPP CRM GitHub deploy`. Under **Certificates & secrets →
   Federated credentials**, add a **GitHub Actions** credential for your repo, entity type
   **Environment**, name `production`.
2. Give it the **Contributor** role on `rg-tpp-crm`, and **AcrPush** on the registry.
3. In the GitHub repo → **Settings**:
   - **Environments** → create `production`, and add yourself as a required reviewer.
   - **Secrets and variables → Actions**:
     - Secrets: `AZURE_CLIENT_ID` (the deploy app's client ID), `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`
     - Variables: `AZURE_ACR_NAME` (e.g. `acrtppcrm`), `AZURE_WEBAPP_NAME` (e.g. `tpp-crm`)
4. **Actions → Deploy to Azure → Run workflow**. It builds the image in Azure, deploys it, and waits for
   `/healthz` to return 200.

## 6. Claude for the team

The web app is ready to host. Connecting Claude to the **hosted** CRM for everyone needs one more decision:

- **Claude custom connector (best end state).** The connector would run as an HTTPS endpoint, and each
  person would sign in with their Microsoft account. Claude connectors use OAuth, and Entra ID doesn't
  support the "dynamic client registration" some connectors expect, so this usually needs either a
  gateway in front (for example Azure API Management) or a Claude plan that lets an admin enter a fixed
  OAuth client ID. Check what your Claude plan and admin settings support before building this.
- **Interim, for one or two admins.** Run the existing local connector against Azure Postgres by setting
  `DATABASE_URL` in the Claude config, plus `TPP_CRM_USER`. It works today, but each person needs
  database credentials and their IP allowed through the firewall, so don't roll this out widely.

## 7. Before inviting the team

- [ ] Clean up overdue deals (124 of 130 open deals had past close dates at import)
- [ ] Data protection: record the CRM in your record of processing activities, agree retention, and note
      Azure UK South as the storage location
- [ ] Confirm backups: Postgres point-in-time restore is on; try a restore once
- [ ] Decide who can delete deals (today anyone signed in can)
- [ ] Turn on branch protection for `main` in GitHub (require CI to pass)
