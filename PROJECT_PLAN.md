# Project Plan: Daily Azure Resource Diff & Diagram Tool (Read-Only)

## 1. Objective
Automatically capture a daily inventory of Azure resources, compare it to the previous day's inventory, identify what was **added**, **removed**, and **modified**, and produce a dependency diagram — all without creating, modifying, or deleting a single Azure resource. The only write action performed is uploading JSON/SVG files to a **pre-existing** storage account's **pre-existing** containers.

## 2. Scope boundary (production-safety)
This is the hard constraint the whole design respects:

| Allowed | Not allowed |
|---|---|
| Read resource metadata via Resource Graph | Create/modify/delete any Azure resource |
| Read resource `properties` to detect relationships | Create the storage account |
| Write blobs to containers you already created | Auto-create containers |
| Log/report what it found | Take any corrective/remediation action |

Practically, this means: **no Terraform, no `az group create`, no `az storage account create`, no auto-provisioning of any kind live in this codebase.** Anything infrastructure-related (the storage account, its containers, and — if you choose to automate this — the Function App itself) is created by you or your platform team, outside this project, using whatever process your organization already governs infrastructure changes with.

## 3. Architecture

```
 ┌─────────────────────┐
 │  (You trigger this)  │  manually, or on a schedule if you deploy it as a Function
 └──────────┬───────────┘
            │
            ▼
 ┌─────────────────────┐        ┌────────────────────────┐
 │ Azure Resource Graph │──read─▶│  (nothing written here — │
 │ (query all resources)│        │   read-only query)        │
 └──────────┬───────────┘        └────────────────────────┘
            │
            ▼
 ┌─────────────────────┐        ┌────────────────────┐
 │  Diff engine          │──────▶│  Your pre-existing   │
 │  (added/removed/mod)  │       │  storage account      │
 └──────────┬───────────┘        │  (containers you       │
            │                    │   already created)     │
            ▼                    │                        │
 ┌─────────────────────┐        │                        │
 │  Diagram builder       │──────▶│                        │
 │  (dependency graph)    │       └────────────────────────┘
 └─────────────────────┘
```

## 4. Components

| Component | Purpose | Write access needed? |
|---|---|---|
| Resource Graph query | Read every resource's metadata + config | No — read-only (`Reader` role) |
| Diff engine | Compare today vs. yesterday | No — in-memory, plain Python |
| Diagram generator | Draw dependency graph + change table (SVG) | No — in-memory |
| Blob upload | Save snapshot/diff/diagram JSON+SVG | Yes — write access **scoped only** to the containers you provide |

## 5. What you provide, once, outside this codebase

- [ ] A storage account (existing, not created by this project)
- [ ] Three blob containers inside it: `snapshots`, `diffs`, `diagrams` (or your own names — created by you via `az storage container create` or the Portal)
- [ ] `Reader` role (for whoever/whatever runs this) on the subscription(s) to be scanned
- [ ] Write access (e.g. `Storage Blob Data Contributor`) scoped to just those three containers

## 6. Rollout phases

### Phase 1 — Local validation
- [ ] Confirm the containers listed above already exist
- [ ] Run `python run_local.py` once to capture a baseline snapshot
- [ ] Run it again the next day (or against a copied "yesterday" snapshot for same-day testing) to confirm the diff and diagram generate correctly

### Phase 2 — Review output with stakeholders
- [ ] Share a sample `diagrams/<date>-diagram.svg` and `diffs/<date>-diff.md` with whoever needs to consume this
- [ ] Confirm the diagram's relationship detection (VM→NIC→VNet/IP/NSG, etc.) covers the resource types actually present in the production environment

### Phase 3 — Optional automation
- [ ] If daily automatic runs are wanted, your platform team provisions a Function App (via your organization's normal IaC/change process — not part of this codebase)
- [ ] Code is deployed into that existing Function App via `func azure functionapp publish`
- [ ] The Function App's managed identity is granted the same minimal `Reader` + scoped storage write access described above

## 7. Security notes
- No credentials are stored in code. `DefaultAzureCredential` resolves to your CLI login locally, or a managed identity if deployed to a Function App.
- `Reader` is the only subscription-level role ever required — sufficient for every Resource Graph query this tool runs.
- Storage write access should be scoped to the specific containers this tool uses, not broader account-level roles, if your organization's RBAC tooling supports container-level scoping.

## 8. File Manifest
```
azure-resource-diff/
├── PROJECT_PLAN.md              <- this file
├── README.md                    <- setup & usage instructions
├── DEPLOYMENT_GUIDE.md / .html  <- step-by-step + script-by-script explanation
├── host.json                    <- Function App host config (only relevant if automated)
├── requirements.txt             <- Python dependencies
├── local.settings.json.example  <- template for local testing
├── run_local.py                 <- standalone runner (no Functions host needed)
└── snapshot_function/
    ├── __init__.py               <- all core logic (read-only query, diff, diagram, write-to-provided-storage)
    └── function.json             <- timer trigger schedule (only relevant if automated)
```
No Terraform, no deployment scripts that create Azure resources — intentionally absent from this project.
