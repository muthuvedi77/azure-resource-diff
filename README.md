# Azure Resource Diff & Diagram Tool (Read-Only)

Fetches your Azure resource inventory, compares it to the previous day, and generates a dependency diagram + change table — saved to a storage account **you already have**.

## Scope and safety guarantees

This tool is intentionally **read-only against your Azure resources** and **does not provision any infrastructure**. Specifically:

- ✅ **Reads** resource metadata via Azure Resource Graph (`Reader` role is sufficient — no write/modify permissions needed on your subscription)
- ✅ **Writes only** to the blob **containers you specify**, inside a storage account **you already have**
- ❌ Does **not** create, modify, or delete any Azure resource, resource group, subscription-level object, or the storage account itself
- ❌ Does **not** auto-create blob containers — they must already exist; the script fails with a clear error if a container is missing, rather than creating one
- ❌ Contains **no infrastructure-as-code** (no Terraform, no `az group create`, no `az storage account create`) — those were removed from this project entirely

## What you need before running this

1. **A storage account you already own**, with three containers already created inside it:
   - one for daily snapshots (e.g. `snapshots`)
   - one for diff reports (e.g. `diffs`)
   - one for diagrams (e.g. `diagrams`)

   Create them yourself, e.g.:
   ```bash
   az storage container create --name snapshots --account-name <your-storage-account>
   az storage container create --name diffs --account-name <your-storage-account>
   az storage container create --name diagrams --account-name <your-storage-account>
   ```
   (This is the only place `az storage container create` is used — run by you directly, not by any script in this project.)

2. **Read access to the subscription(s) you want scanned** — the `Reader` role on your Azure CLI login (or Function App identity, if you deploy it that way) is sufficient.

## Local Setup & Test

```bash
python -m venv .venv
source .venv/Scripts/activate      # Windows/Git Bash
pip install -r requirements.txt

cp local.settings.json.example local.settings.json
# edit local.settings.json with your storage connection string, container names, subscription ID
```

`run_local.py` reads configuration from real environment variables, not `local.settings.json` directly (that file is only auto-read by the Azure Functions host). Export them in your shell before running:

```bash
export STORAGE_CONNECTION_STRING="<your connection string>"
export SUBSCRIPTION_IDS="<your subscription id>"
export CONTAINER_SNAPSHOTS="snapshots"
export CONTAINER_DIFFS="diffs"
export CONTAINER_DIAGRAMS="diagrams"

python run_local.py
```

First run saves today's snapshot only (nothing to diff against yet). From the second day onward, it also produces a diff report and diagram.

## What gets written, and where

| Container | Contents |
|---|---|
| `snapshots/` | `YYYY-MM-DD.json` — full resource inventory for that day |
| `diffs/` | `YYYY-MM-DD-diff.json` and `.md` — added/removed/modified resources |
| `diagrams/` | `YYYY-MM-DD-diagram.svg` — dependency-graph diagram + change table |

Nothing is written anywhere else. No resource groups, VMs, storage accounts, or any other Azure object is ever created, modified, or deleted by this code.

## Automating it (optional)

If you want this to run on a schedule instead of manually:
- `snapshot_function/function.json` and `host.json` are included, set up for an Azure Functions **Timer Trigger**.
- Deploying this to a Function App still requires the Function App itself to exist first — that's a one-time provisioning step **you** perform (via the Azure Portal, your own IaC, or your platform team's standard process), deliberately kept **outside** this project so the codebase itself never contains resource-creation logic.
- Once a Function App exists, deploy the code into it with:
  ```bash
  func azure functionapp publish <your-function-app-name> --python
  ```
- Grant that Function App's identity: `Reader` on the subscription(s) to scan, and write access (e.g. `Storage Blob Data Contributor`) scoped to the storage account's containers above — again, done by you/your platform team, not by any script here.

## Troubleshooting

- **"Container 'X' does not exist"** — expected and intentional; create the container yourself first (see above), then re-run.
- **No diff produced on first run** — expected; nothing to compare against until a second day's snapshot exists.
- **`AuthorizationFailed`** — confirm your login (or the Function App's identity) has `Reader` on the subscription, and write access on the storage account's containers.
