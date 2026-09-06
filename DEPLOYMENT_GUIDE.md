# Deployment Guide — Steps and Script Explanations

This document explains **what** each step does, **why** it's necessary, and **what every file in this project actually does**. It reflects the current, production-safe version: **read-only against your Azure resources**, writing only to a storage account you already own.

---

## Part 1 — Scope and design principle

This tool follows one hard rule: **it never creates, modifies, or deletes an Azure resource other than blobs inside containers you already created.**

```
Resource Graph query   →  READ ONLY  (Reader role, nothing more)
Diff + diagram logic   →  in-memory, no Azure calls at all
Blob upload             →  WRITE, but only to containers you pre-created
```

There is intentionally **no Terraform, no `az group create`, no `az storage account create`, and no auto-creation of blob containers** anywhere in this codebase. If a container you specified doesn't exist, the script fails with a clear error instead of creating one — this is deliberate, not a bug.

The one piece of infrastructure this *does* eventually need — if you choose to automate it — is the **Function App itself** (the compute shell that runs the code on a timer). That is still something you or your platform team provisions once, outside this codebase, using whatever process your organization already uses for infrastructure changes.

---

## Part 2 — Step-by-step: local test run

### Step 1: Create the storage containers yourself (one-time, manual)
```bash
az storage container create --name snapshots --account-name <your-storage-account>
az storage container create --name diffs --account-name <your-storage-account>
az storage container create --name diagrams --account-name <your-storage-account>
```
**Why:** The script deliberately does not do this for you — see Part 1. This is the only "creation" step in the entire workflow, and it's run by you directly against your own storage account, not embedded in any script.

### Step 2: Set up Python locally
```bash
python -m venv .venv
source .venv/Scripts/activate
pip install -r requirements.txt
```
**Why:** Isolates this project's dependencies from anything else on your machine.

### Step 3: Export configuration as environment variables
```bash
export STORAGE_CONNECTION_STRING="<your connection string>"
export SUBSCRIPTION_IDS="<your subscription id>"
export CONTAINER_SNAPSHOTS="snapshots"
export CONTAINER_DIFFS="diffs"
export CONTAINER_DIAGRAMS="diagrams"
```
**Why:** `run_local.py` bypasses the Azure Functions runtime entirely, so it reads real shell environment variables — not `local.settings.json` (that file is only auto-loaded by the Functions host, a separate execution path used later for automation).

### Step 4: Run it
```bash
python run_local.py
```
**What happens:** Queries Resource Graph (read-only) → saves today's snapshot → loads yesterday's snapshot if present → diffs them → builds the dependency diagram + 3-column table → uploads results to your storage account.

---

## Part 3 — Every script, explained

### `snapshot_function/__init__.py` — the core logic

**`RESOURCE_GRAPH_QUERY`**
Selects `id, name, type, resourceGroup, location, subscriptionId, tags, kind, sku, properties` from every resource. `properties` is included specifically so the diagram code can read a VM's attached NIC/disk, and a NIC's subnet/public IP/NSG — plain metadata alone doesn't reveal relationships, only each resource's own attributes.

**`get_resource_graph_client()` / `get_blob_service_client()`**
Both use `DefaultAzureCredential()`, which automatically resolves to your `az login` session locally, or a managed identity if this later runs inside a Function App — same code, no changes needed between the two environments, and no secrets ever hardcoded.

**`get_container()`**
Read-only-safe by design: checks a container exists and returns a client for it. **Does not create anything.** Raises a clear `RuntimeError` naming the missing container if it isn't found, rather than silently creating one — this is the guardrail that keeps the whole tool from ever provisioning storage infrastructure on its own.

**`query_all_resources()`**
Runs the KQL query, paging through results 1,000 rows at a time via Resource Graph's `skip_token` mechanism, since a subscription can have more resources than one response holds.

**`save_snapshot()` / `load_snapshot()`**
Write/read the daily inventory as a dated JSON blob (`2026-09-03.json`). Every day's snapshot is kept (not just the latest), giving a full historical trail.

**`diff_snapshots()`**
Pure set comparison, matched by Azure's `id` (globally unique, unlike `name` which can repeat across resource groups). Added = new IDs, Removed = missing IDs, Modified = same ID but changed `location`/`tags`/`sku`/`kind`/`resourceGroup`.

**`save_diff_report()`**
Writes the diff as both `.json` (machine-readable) and `.md` (human-readable) so it can be consumed by tooling or just opened and read directly.

**`_extract_edges()`**
Parses each resource's raw `properties` for known reference patterns to detect real relationships:
- VM → its NIC (`networkProfile.networkInterfaces[].id`)
- VM → its OS disk (`storageProfile.osDisk.managedDisk.id`)
- NIC → its VNet (derived from `ipConfigurations[].properties.subnet.id`)
- NIC → its Public IP and NSG

This is what lets the diagram show actual dependency arrows (matching Azure's own Resource Visualizer style) instead of just an unconnected list of boxes.

**`generate_diagram_svg()`**
Builds the visual output in two parts, as one SVG file:
1. **Dependency graph** — resources laid out in tiers by relationship depth, connected by arrows, color-coded by change status (green border=added, red=removed, amber=modified), with a colored icon per resource type (VM, NIC, VNet, NSG, etc.)
2. **3-column table** beneath the graph:
   - **Current Network Resources** — every `Microsoft.Network/*` resource that exists today, listed alphabetically, regardless of change status
   - **Newly Added** — every resource (any type) present today but not in yesterday's snapshot
   - **Newly Deleted** — every resource (any type) present yesterday but missing today

   *(Note: "Current Network Resources" is filtered to network resource types specifically, per your request; the Added/Deleted columns cover all resource types, not just network ones — flag if you'd rather those be network-filtered too.)*

**`save_diagram()`**
Uploads the finished SVG with `image/svg+xml` content type, so it renders correctly if opened directly in a browser.

**`main(mytimer)`**
The entry point Azure Functions calls on schedule. Ties every step together: query → snapshot → diff → diagram → upload, then logs a one-line summary (`+X / -X / ~X`).

### `run_local.py`
Imports the same functions above and calls them directly, without the Azure Functions runtime. This is what you use for manual/local testing — same underlying logic as the scheduled version, just triggered by you instead of a timer.

### `snapshot_function/function.json`
```json
"schedule": "0 0 10 * * *"
```
6-field NCRONTAB (`second minute hour day month day-of-week`). `10:00 UTC` = **6:00 PM Singapore Time**. This only takes effect once the code is deployed into an actual Function App (see Part 4) — it has no effect when running via `run_local.py`.

### `host.json`
App-wide Azure Functions host configuration (logging, extension bundle version). Only relevant once deployed to a Function App; unused by `run_local.py`.

### `requirements.txt`
Python dependencies: `azure-functions`, `azure-identity`, `azure-mgmt-resourcegraph`, `azure-storage-blob`. Read by both `pip install -r requirements.txt` (local) and the Function App's deployment process (remote).

### `local.settings.json.example`
Template for the Azure Functions host's local settings — only auto-read if you run this via `func start` (not used in our testing, which used `run_local.py` + exported env vars instead).

---

## Part 4 — Automating it (one-time setup, outside this codebase)

To have this run daily at 6pm SGT without manual triggering, a Function App must exist first. This is infrastructure creation — deliberately **not** part of this script — so it's a manual, one-time setup:

### Step 1: Create the Function App shell
```bash
az group create --name rg-resource-diff-automation --location southeastasia
az storage account create --name <func-hosting-storage> --resource-group rg-resource-diff-automation --location southeastasia --sku Standard_LRS
az functionapp create --name <your-function-app-name> --resource-group rg-resource-diff-automation \
  --storage-account <func-hosting-storage> --consumption-plan-location southeastasia \
  --runtime python --runtime-version 3.11 --functions-version 4 --os-type Linux
```
**Why a separate storage account here:** this one is the Function App's own internal requirement (Azure Functions needs storage to manage its own state) — it is *not* the same storage account you're saving diagrams to. Keeping them separate avoids any confusion about what the tool reads/writes versus what Azure's runtime needs to operate.

### Step 2: Grant minimal permissions
```bash
az functionapp identity assign --name <your-function-app-name> --resource-group rg-resource-diff-automation
PRINCIPAL_ID=$(az functionapp identity show --name <your-function-app-name> --resource-group rg-resource-diff-automation --query principalId -o tsv)

az role assignment create --assignee "$PRINCIPAL_ID" --role "Reader" --scope "/subscriptions/<production-subscription-id>"

STORAGE_ID=$(az storage account show --name <your-diagram-storage-account> --resource-group <its-rg> --query id -o tsv)
az role assignment create --assignee "$PRINCIPAL_ID" --role "Storage Blob Data Contributor" --scope "$STORAGE_ID"
```
**Why `Reader` only:** it's the minimum permission Resource Graph queries need — no write/modify access to production resources is ever granted.
**Why scope the storage role to the storage account ID, not the subscription:** limits write access to exactly the one account this tool needs, nothing broader.

### Step 3: Point it at your storage account
```bash
az functionapp config appsettings set --name <your-function-app-name> --resource-group rg-resource-diff-automation \
  --settings STORAGE_CONNECTION_STRING="<your diagram storage connection string>" \
             CONTAINER_SNAPSHOTS="snapshots" CONTAINER_DIFFS="diffs" CONTAINER_DIAGRAMS="diagrams" \
             SUBSCRIPTION_IDS="<production-subscription-id>"
```

### Step 4: Deploy the code
```bash
func azure functionapp publish <your-function-app-name> --python
```
This pushes the code into the already-created Function App and installs its Python dependencies remotely — a separate action from provisioning, since Terraform/CLI has no concept of "your application logic."

### From here on
Azure's Functions host wakes the code on the 10:00 UTC (6pm SGT) timer, runs it, uploads the diagram, and goes back to sleep — no manual triggering, no laptop needing to be on.

---

## Part 5 — What runs automatically vs. what you do once

| Action | Frequency |
|---|---|
| Create storage containers | Once |
| Create the Function App shell + grant permissions | Once |
| `func azure functionapp publish` | Once (or whenever code changes) |
| Query → snapshot → diff → diagram → upload | Every day at 6pm SGT, automatically, forever |

From here, your only ongoing task is checking the `diagrams/` container whenever you want to see the latest result.
