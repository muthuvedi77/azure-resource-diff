# Runbook — Azure Resource Diff & Diagram Tool

**Your deployed environment:**

| Value | Setting |
|---|---|
| Resource Group | `rg-terraform-state` |
| Storage Account | `sttfstatemigration` |
| Function App name | `functionapp1-mtdiff4728` |
| Subscription ID | `317b92c9-a532-4f6a-80f2-abe8b6770eb4` |
| Region | `southeastasia` |
| Schedule | `16:00 UTC` daily = **12:00 AM (Midnight) Singapore Time** |

Every command below states its **purpose** so you know what it does and why it's there — not just what to type.

---

## Part A — One-time setup (already completed, kept here for reference/redeploying elsewhere)

### 1. Confirm blob containers exist
```bash
az storage container list --account-name sttfstatemigration --output table
```
**Purpose:** verifies `snapshots`, `diffs`, `diagrams` already exist. The tool never creates containers itself — it fails with a clear error if one is missing, by design (see Part F).

### 2. Register the Web resource provider (once per subscription)
```bash
az provider register --namespace Microsoft.Web
az provider show --namespace Microsoft.Web --query registrationState -o tsv
```
**Purpose:** Azure Functions/App Service resources live under the `Microsoft.Web` namespace. A subscription that's never used Functions before needs this "switched on" first — a one-time, free, non-resource-creating action. Repeat the second command until it shows `Registered`.

### 3. Create the Function App
```bash
az functionapp create \
  --name functionapp1-mtdiff4728 \
  --resource-group rg-terraform-state \
  --storage-account sttfstatemigration \
  --consumption-plan-location southeastasia \
  --runtime python \
  --runtime-version 3.11 \
  --functions-version 4 \
  --os-type Linux
```
**Purpose:** creates the compute shell that will run your Python code on a timer. `functionapp1` alone was already taken by someone else globally (names must be unique across *all* of Azure) — `functionapp1-mtdiff4728` is the actual name that succeeded.

### 4. Assign the Function App a managed identity
```bash
az functionapp identity assign --name functionapp1-mtdiff4728 --resource-group rg-terraform-state

PRINCIPAL_ID=$(az functionapp identity show --name functionapp1-mtdiff4728 --resource-group rg-terraform-state --query principalId -o tsv)
echo $PRINCIPAL_ID
```
**Purpose:** creates an Azure-managed "robot identity" tied to this Function App, so the deployed code can authenticate without any stored password/key. `echo` confirms it worked — must print a GUID.

### 5. Grant minimum permissions
```bash
MSYS_NO_PATHCONV=1 az role assignment create \
  --assignee-object-id "$PRINCIPAL_ID" \
  --assignee-principal-type ServicePrincipal \
  --role "Reader" \
  --scope "/subscriptions/317b92c9-a532-4f6a-80f2-abe8b6770eb4"
```
**Purpose:** gives the identity **read-only** visibility into the subscription — the minimum required for Resource Graph queries. `MSYS_NO_PATHCONV=1` stops Git Bash from mangling the `/subscriptions/...` path into a Windows-style path. `--assignee-object-id` + `--assignee-principal-type` avoids a lookup failure that plain `--assignee` hits on brand-new identities.

```bash
STORAGE_ID=$(az storage account show --name sttfstatemigration --resource-group rg-terraform-state --query id -o tsv)

MSYS_NO_PATHCONV=1 az role assignment create \
  --assignee-object-id "$PRINCIPAL_ID" \
  --assignee-principal-type ServicePrincipal \
  --role "Storage Blob Data Contributor" \
  --scope "$STORAGE_ID"
```
**Purpose:** gives the identity write access, but **scoped only to this one storage account** — not the whole subscription — so it can upload snapshots/diffs/diagrams and nothing more.

### 6. Set the storage connection string
```bash
CONN_STR=$(az storage account show-connection-string \
  --name sttfstatemigration \
  --resource-group rg-terraform-state \
  --query connectionString -o tsv)
echo ${#CONN_STR}

az functionapp config appsettings set \
  --name functionapp1-mtdiff4728 \
  --resource-group rg-terraform-state \
  --settings STORAGE_CONNECTION_STRING="$CONN_STR"
```
**Purpose:** tells the deployed code which storage account to write results into. Loading into `$CONN_STR` first (rather than pasting the raw string directly) avoids Git Bash mangling the semicolon-separated string — this was the actual cause of a `ValueError: Connection string is either blank or malformed` failure during setup. `echo ${#CONN_STR}` should print ~250-300 to confirm the string is intact before using it.

### 7. Set the remaining app settings
```bash
az functionapp config appsettings set \
  --name functionapp1-mtdiff4728 \
  --resource-group rg-terraform-state \
  --settings \
    CONTAINER_SNAPSHOTS="snapshots" \
    CONTAINER_DIFFS="diffs" \
    CONTAINER_DIAGRAMS="diagrams" \
    SUBSCRIPTION_IDS="317b92c9-a532-4f6a-80f2-abe8b6770eb4"
```
**Purpose:** tells the code which containers to use and which subscription to scan.

### 8. Confirm the schedule
```bash
cat /c/Projects/azure-resource-diff/snapshot_function/function.json
```
**Purpose:** verifies the timer is set to `"schedule": "0 0 16 * * *"` (16:00 UTC = 12:00 AM Midnight Singapore Time) before deploying. If this errors with "No such file," you likely have a nested duplicate folder from re-extracting a zip — check with `ls /c/Projects/azure-resource-diff` and flatten if needed.

### 9. Install Azure Functions Core Tools
```bash
winget install Microsoft.Azure.FunctionsCoreTools
```
**Purpose:** installs the `func` CLI — a separate tool from `az`, specifically needed to package and upload your Python code into the Function App. **Close and reopen Git Bash completely** afterward, then confirm with `func --version`.

### 10. Deploy the code
```bash
cd /c/Projects/azure-resource-diff
func azure functionapp publish functionapp1-mtdiff4728 --python
```
**Purpose:** the actual "make it run" step — zips your code and remotely installs `requirements.txt` inside the Function App. This is separate from provisioning; Terraform/`az` has no concept of "your application logic."

### 11. Confirm deployment succeeded
```bash
az functionapp function list --name functionapp1-mtdiff4728 --resource-group rg-terraform-state --output table
```
**Purpose:** confirms `snapshot_function` is actually registered inside the deployed app, with `IsDisabled: False`.

---

## Part B — How to manually trigger a test run (any time)

### 1. Get the master key
```bash
az functionapp keys list \
  --name functionapp1-mtdiff4728 \
  --resource-group rg-terraform-state \
  --query masterKey -o tsv
```
**Purpose:** retrieves the credential needed to call the function's admin trigger endpoint directly.

### 2. Trigger the function immediately
```bash
curl -X POST \
  "https://functionapp1-mtdiff4728.azurewebsites.net/admin/functions/snapshot_function" \
  -H "x-functions-key: <paste master key>" \
  -H "Content-Type: application/json" \
  -d '{}'
```
**Purpose:** forces one run right now instead of waiting for the 6pm schedule. `az functionapp function invoke` is **not** a real command — this Admin API call is the correct method. An empty response is normal; it doesn't print a confirmation even on success.

### 3. Confirm it ran
```bash
az storage blob list --account-name sttfstatemigration --container-name snapshots --output table
az storage blob list --account-name sttfstatemigration --container-name diffs --output table
az storage blob list --account-name sttfstatemigration --container-name diagrams --output table
```
**Purpose:** looks for today's date with a fresh "Last Modified" timestamp in each container — proof the whole pipeline (query → snapshot → diff → diagram → upload) completed successfully.

---

## Part C — How to test change-detection (added / deleted resources)

This is the exact sequence that confirmed both Added and Deleted detection work correctly.

### Test an addition
```bash
az network vnet create \
  --name Test-Vnet-3 \
  --resource-group Test-RG-Automation \
  --address-prefix 10.1.0.0/16 \
  --subnet-name default \
  --subnet-prefix 10.1.0.0/24 \
  --location southeastasia
```
**Purpose:** creates a small, disposable resource to prove the tool detects new resources. Wait 30-60 seconds for Resource Graph to index it before triggering the function (Part B).

### Test a deletion
```bash
az network vnet delete --name Test-Vnet-3 --resource-group Test-RG-Automation
```
**Purpose:** removes the test resource. For the *next* run to correctly show it as "Removed," the snapshot **immediately before** this deletion must already contain it — i.e., trigger the function once *before* deleting (capturing it as present), then delete, then trigger again (capturing it as gone). A deletion can only be detected between two of the tool's own real snapshots — anything deleted before the tool ever ran is invisible to it (expected behavior, not a bug).

### Check the diff after either test
```bash
az storage blob download --account-name sttfstatemigration --container-name diffs --name "<todays-date>-diff.json" --file diff-check.json
cat diff-check.json
```
**Purpose:** confirms detection at the data level — look for the resource under `"added"` or `"removed"`, and check `summary.added_count` / `summary.removed_count`.

### View the diagram
```bash
az storage blob download --account-name sttfstatemigration --container-name diagrams --name "<todays-date>-diagram.svg" --file diagram-check.svg
start diagram-check.svg
```
**Purpose:** confirms the visual output — the resource should appear color-coded (green border = Added) in the dependency graph, and listed in the correct column of the 3-column table at the bottom (Current Network Resources / Newly Added / Newly Deleted).

---

## Part D — Ongoing health checks

### Quick check: did today's scheduled run happen?
```bash
az storage blob list --account-name sttfstatemigration --container-name diagrams --output table
```
**Purpose:** confirms the 6pm automatic trigger fired — look for today's date with a recent timestamp.

### Check for errors, if a run seems to have failed
Portal → search **Application Insights** → click `functionapp1-mtdiff4728` → **Logs**, then run:
```kusto
exceptions
| where timestamp > ago(1h)
| order by timestamp desc
| project timestamp, problemId, outerMessage, details
```
**Purpose:** the Function App's own Functions/Monitor tab isn't always visible depending on the portal view — Application Insights Logs reliably shows every exception with full detail.

---

## Part E — Rotating the storage key (if ever exposed)

```bash
az storage account keys renew --account-name sttfstatemigration --resource-group rg-terraform-state --key primary
az storage account keys renew --account-name sttfstatemigration --resource-group rg-terraform-state --key secondary
```
**Purpose:** invalidates any previously-exposed key. After rotating, repeat **Part A, Step 6** (re-fetch connection string into `$CONN_STR`, then update the app setting) — the deployed function stops working until this is redone, since it's still holding the old key.

---

## Part F — Design principle (why some steps look the way they do)

This tool is intentionally **read-only against your Azure resources**:
- It only ever reads via Resource Graph (`Reader` role — no write/modify permissions on production resources, ever)
- It only ever writes blobs into containers **you** created in advance
- It does **not** auto-create containers, resource groups, VMs, or anything else — if a container is missing, it errors with a clear message instead of creating one

The Function App itself (Part A) is the one piece of infrastructure this setup needs to exist — that's the tool's own hosting shell, provisioned once by you, and is separate from the production resources being monitored.

---

## Part G — Full diagnosis table

| # | Symptom | Cause | Fix |
|---|---|---|---|
| 1 | `MissingSubscriptionRegistration ... Microsoft.Web` | Subscription never used Azure Functions before | `az provider register --namespace Microsoft.Web` |
| 2 | `Website with given name functionapp1 already exists` | Function App names are globally unique across all of Azure | Use a more unique name |
| 3 | `Cannot find user or service principal in graph database` | Plain `--assignee` fails on a brand-new managed identity | Use `--assignee-object-id` + `--assignee-principal-type ServicePrincipal` |
| 4 | `MissingSubscription: The request did not have a subscription...` | Git Bash rewrites `/subscriptions/...` into a Windows path | Prefix with `MSYS_NO_PATHCONV=1` |
| 5 | `ResourceNotFound ... functionapp1` | Typed the old (taken) name instead of the real one | Always use `functionapp1-mtdiff4728` |
| 6 | `cat: function.json: No such file or directory` | Nested duplicate folder from re-extracting a zip | Check with `ls`, flatten with `mv`/`rmdir` |
| 7 | `bash: func: command not found` | Functions Core Tools is separate from Azure CLI | `winget install Microsoft.Azure.FunctionsCoreTools`, reopen terminal |
| 8 | `'invoke' is misspelled or not recognized` | `az functionapp function invoke` isn't a real command | Use the Admin API via `curl` + master key |
| 9 | `ValueError: Connection string is either blank or malformed` | Raw string mangled when pasted directly into `--settings` | Load into `$CONN_STR` first, then use `"$CONN_STR"` |
| 10 | Diff shows a deletion as missing (`removed: []`) even though a resource was deleted | The resource was deleted **before** the tool's very first snapshot — nothing to compare it against | Expected behavior; only deletions happening *between two of the tool's own snapshots* can be detected |
| 11 | Manual trigger via `curl` with `-H "x-functions-key: ..."` header keeps returning `401 Unauthorized` with `WWW-Authenticate: Bearer`, even with a correct key and Easy Auth confirmed **off** | This specific admin route (`/admin/functions/<name>`) doesn't reliably accept the key as a header on this app/environment | Pass the key as a **query parameter** instead: `.../admin/functions/snapshot_function?code=$MASTER_KEY` — this succeeded (`202 Accepted`) where the header form kept failing |
| 12 | Redeploying and re-triggering repeatedly produces the **same diagram, same timestamp**, no matter what | Local `snapshot_function/__init__.py` was an **older version** — `func publish` was deploying outdated code correctly, so nothing looked "wrong" in the deploy logs themselves | Before redeploying, always verify the local file actually has the latest change: `grep -c "<a string unique to the latest update>" snapshot_function/__init__.py` and confirm it's non-zero before running `func publish` |
| 13 | Commands suddenly fail with "No such file or directory" for paths that worked minutes earlier | Terminal prompt had silently changed to a **different machine** (e.g. `azureuser@terraform-vm` instead of `hp@DESKTOP-80492DK`) — likely from an earlier SSH/remote session left open | Always check the prompt / run `hostname` if a previously-working path suddenly "disappears" |
| 14 | A pasted `curl` command with a stored key variable shows an extra stray character (e.g. `$2AkgwbU...`) in the request | Manual retyping/copy-paste of a shell variable reference introduced an accidental character before the `$` | Always reference the key purely by variable (`"$MASTER_KEY"`), never retype or partially copy the expanded value; verify with `echo "Key length: ${#MASTER_KEY}"` before using it |
| 15 | Icon files exist locally, deploy succeeds, but the diagram still shows colored-glyph circles instead of real icons | Local folder was named `Icons` (capital "I"); Windows is case-insensitive so this looked fine locally, but the deployed **Linux** container is case-sensitive and the code looks for lowercase `icons` | Rename the folder to exactly `icons` (`mv Icons icons`), then redeploy — confirmed by the diagram's file size roughly tripling (~8.7KB → ~28KB) once real icon images are embedded |

---

## Part H — Container reference: what's in the storage account, and who put it there

Since `sttfstatemigration` is a **reused, pre-existing** storage account (originally for Terraform state), it holds a mix of containers — some are ours, some pre-existed, and some were auto-created by Azure Functions itself. Knowing which is which matters for cleanup (Part I) and for not accidentally touching something unrelated.

| Container | Created by | Purpose |
|---|---|---|
| `snapshots` | **Our tool** | Daily resource inventory JSON files |
| `diffs` | **Our tool** | Daily change reports — `.json` (machine-readable) + `.md` (human-readable) |
| `diagrams` | **Our tool** | Daily dependency-graph SVG diagrams with the 3-column change table |
| `tfstate` | **Pre-existing** (before this project) | Terraform's own state file storage — this is the original reason this storage account/resource group exists. **Not related to our tool.** |
| `azure-webjobs-hosts` | Auto-created by Azure Functions runtime | Internal bookkeeping — timer schedule state, locks, "last run" tracking |
| `azure-webjobs-secrets` | Auto-created by Azure Functions runtime | Stores the Function App's internal keys (e.g. the master key used to trigger it manually) |
| `scm-releases` | Auto-created during deployment | Holds a history of the zip packages uploaded by each `func azure functionapp publish` |
| `$logs` | Auto-created by Azure Storage | System-level storage diagnostic logs — a default Storage feature, unrelated to our tool |

**⚠️ Never delete `tfstate` or `$logs`** — both are unrelated to this project and deleting `tfstate` could break existing Terraform-managed infrastructure elsewhere.

---

## Part I — Full teardown (destroy everything this project created)

**Do NOT delete:** `rg-terraform-state` (resource group) or `sttfstatemigration` (storage account) themselves — both existed before this project; we only reused them.

### 1. Delete the Function App
```bash
az functionapp delete --name functionapp1-mtdiff4728 --resource-group rg-terraform-state
```
**Purpose:** removes the Function App and its managed identity (usually removes its Consumption plan automatically too).

### 2. Confirm the App Service Plan is gone
```bash
az functionapp plan list --resource-group rg-terraform-state --output table
```
If a leftover plan remains with no apps using it:
```bash
az functionapp plan delete --name <plan-name-shown-above> --resource-group rg-terraform-state --yes
```

### 3. Delete the Application Insights component
```bash
az monitor app-insights component delete --app functionapp1-mtdiff4728 --resource-group rg-terraform-state
```

### 4. Clean up orphaned role assignments (optional, good hygiene)
```bash
az role assignment list --resource-group rg-terraform-state --output table
```
Delete any tied to the now-deleted identity:
```bash
az role assignment delete --ids <assignment-id-shown-above>
```

### 5. Delete the test resource group (if it still exists)
```bash
az group delete --name Test-RG-Automation --yes --no-wait
```

### 6. Clean up the Functions-runtime-created containers (Part H)
```bash
az storage container delete --account-name sttfstatemigration --name azure-webjobs-hosts
az storage container delete --account-name sttfstatemigration --name azure-webjobs-secrets
az storage container delete --account-name sttfstatemigration --name scm-releases
```

### 7. Clear out our own data containers (optional — keeps containers, empties contents for a clean rebuild)
```bash
az storage blob delete-batch --account-name sttfstatemigration --source snapshots
az storage blob delete-batch --account-name sttfstatemigration --source diffs
az storage blob delete-batch --account-name sttfstatemigration --source diagrams
```

### 8. Verify everything's clean
```bash
az functionapp list --resource-group rg-terraform-state --output table
az group show --name Test-RG-Automation --output table
az storage blob list --account-name sttfstatemigration --container-name diagrams --output table
```
First two should return empty/error (gone); last should show an empty list.

**Cost note:** at this usage level (a handful of manual test runs over a couple of days, no VMs, no gateways/public IPs), the actual cost impact of everything this project created is realistically **$0.00–$0.05 USD** — Azure Functions Consumption plan, Application Insights, and blob storage all fall well within their free monthly allowances at this scale.

---

## Part K — Which resource types the diagram recognizes (and how to add more)

### The diff/change-table logic tracks EVERY resource type automatically
"Added / Removed / Modified" and the "Current Network Resources" table column work on **any** resource in your subscription, with no hardcoded list — VNets, Subnets-as-part-of-a-VNet, Application Gateways, Firewalls, anything at all shows up correctly there.

### The diagram's icons + relationship arrows only recognize a fixed set of 9 types
These live in `snapshot_function/__init__.py`, in two places that must be kept in sync:

**1. `TYPE_STYLE` (~line 212)** — fallback colored circle + glyph, used if no icon file exists:
```python
TYPE_STYLE = {
    "microsoft.compute/virtualmachines":       {"glyph": "VM",  "color": "#0078d4"},
    "microsoft.compute/disks":                 {"glyph": "DISK", "color": "#106ebe"},
    "microsoft.network/networkinterfaces":     {"glyph": "NIC", "color": "#107c10"},
    "microsoft.network/virtualnetworks":       {"glyph": "VNET", "color": "#00b294"},
    "microsoft.network/publicipaddresses":     {"glyph": "IP",  "color": "#5c2d91"},
    "microsoft.network/networksecuritygroups": {"glyph": "NSG", "color": "#005a9e"},
    "microsoft.network/networkwatchers":       {"glyph": "NW",  "color": "#00bcf2"},
    "microsoft.storage/storageaccounts":       {"glyph": "ST",  "color": "#00838f"},
    "microsoft.resources/subscriptions/resourcegroups": {"glyph": "RG", "color": "#8c959f"},
}
```

**2. `ICON_FILES` (~line 233)** — maps the same types to official Azure icon filenames (see Part A/icon setup):
```python
ICON_FILES = {
    "microsoft.compute/virtualmachines":       "virtual-machine.svg",
    "microsoft.compute/disks":                 "disk.svg",
    "microsoft.network/networkinterfaces":     "network-interface.svg",
    "microsoft.network/virtualnetworks":       "virtual-network.svg",
    "microsoft.network/publicipaddresses":     "public-ip-address.svg",
    "microsoft.network/networksecuritygroups": "network-security-group.svg",
    "microsoft.network/networkwatchers":       "network-watcher.svg",
    "microsoft.storage/storageaccounts":       "storage-account.svg",
    "microsoft.resources/subscriptions/resourcegroups": "resource-group.svg",
}
```

**What happens for a type NOT in either list** (e.g. Application Gateway, Azure Firewall right now): it still appears in the diagram as a box, but with the generic `DEFAULT_TYPE_STYLE` gray "RES" circle, and **no relationship arrows** — because arrow-drawing depends on `_extract_edges()` (~line 284) knowing how to read that specific resource type's `properties` for references to other resources.

### Currently NOT tracked with arrows/icons: Application Gateway, Azure Firewall, Subnets
- **Application Gateway** (`microsoft.network/applicationgateways`) and **Azure Firewall** (`microsoft.network/azurefirewalls`) — appear as generic gray boxes; no icon, no arrows to their attached VNet/subnet/public IP yet.
- **Subnets** — not a top-level Resource Graph entry at all; they live nested inside a VNet's `properties.subnets[]` array, so only the parent VNet shows as a node today, not each individual subnet.

### To add a new resource type (e.g. Application Gateway)

1. **Add an icon file** to `snapshot_function/icons/` (download from Microsoft's icon set, rename to e.g. `application-gateway.svg`)
2. **Add one line to `TYPE_STYLE`:**
   ```python
   "microsoft.network/applicationgateways": {"glyph": "AGW", "color": "#8661c5"},
   ```
3. **Add one line to `ICON_FILES`:**
   ```python
   "microsoft.network/applicationgateways": "application-gateway.svg",
   ```
4. **(Optional, for arrows)** extend `_extract_edges()` to parse that resource type's `properties` for references — e.g. an Application Gateway's `properties.gatewayIPConfigurations[].properties.subnet.id` would need similar handling to how NIC→VNet is currently resolved.
5. **Redeploy:**
   ```bash
   cd /c/Projects/azure-resource-diff
   func azure functionapp publish functionapp1-mtdiff4728 --python
   ```

Without step 4, the new resource type will still show up correctly (with its own icon, correct Added/Removed/Modified coloring) — it just won't have arrows connecting it to related resources, which is often acceptable depending on what you need the diagram to show.

---

## Part M — RESOLVED: Official Microsoft Azure icon images now appear in the diagram

**Final status:** ✅ Fixed and confirmed working. The diagram now shows real Microsoft Azure icon artwork (not colored-glyph circles) for every resource type with an icon file present.

### Root cause
**Windows/Linux filesystem case-sensitivity.** The local icons folder was created as `Icons` (capital "I") on Windows. Windows filesystems are case-insensitive, so `Icons` and `icons` are treated as identical locally — everything looked fine on the laptop. The deployed Function App runs on a **Linux** container, which **is** case-sensitive: the code's `ICON_DIR = os.path.join(os.path.dirname(__file__), "icons")` (lowercase) never matched the deployed `Icons` (capital) folder, so every icon lookup silently failed and fell back to the colored glyph — with no error thrown anywhere, which is why it took extensive debugging to surface.

### How it was found
Temporary debug logging was added to `main()` to print `ICON_DIR`, whether it exists, and (if not) the parent folder's actual contents. Triggering the function and checking **Application Insights → Logs** (`traces | where message startswith "DEBUG"`) showed the smoking gun directly:
```
DEBUG: parent dir contents: ['Icons', '__init__.py', 'function.json']
```
Capital "I" — folder name mismatch, immediately obvious once visible.

### The fix
```bash
cd /c/Projects/azure-resource-diff/snapshot_function
mv Icons icons
```
Then redeploy (`func azure functionapp publish functionapp1-mtdiff4728 --python`). Confirmed working: the diagram's file size jumped from ~8.7 KB to ~28 KB after the fix (embedded base64 icon images are significantly larger than simple SVG shapes) — a reliable quick indicator the fix took effect, in addition to visually confirming the real icons in the output.

### Lesson for the future
**On Windows, folder/file names can silently differ in case from what the code expects, with zero local symptoms** — this will only surface once deployed to a Linux-based Function App. When creating any folder the deployed code will reference by exact name, double-check the case matches precisely, or better, confirm with `ls` immediately after creation rather than assuming.

### The debug logging is still in the code
It's harmless to leave in (just extra log lines each run) but can be removed from `main()` in `snapshot_function/__init__.py` (search for `# --- TEMPORARY DEBUG` through `# --- END TEMPORARY DEBUG ---`) if you want cleaner logs going forward.

---

## Part N — What's automatic vs. what you'd only touch again

| Action | Frequency |
|---|---|
| Part A (Steps 1-11) | **Once**, already done |
| Redeploy code (`func azure functionapp publish`) | Only if the Python code changes |
| Update app settings (Part A Step 6/7) | Only if the storage key rotates or config changes |
| Query → snapshot → diff → diagram → upload | **Every day at 6:00 PM SGT, automatically, forever** |

From here, your only ongoing task is checking the `diagrams` container whenever you want to see the latest result.
