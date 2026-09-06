# Azure Resource Diff & Diagram Tool — Overview for Management

## 1. What this project does

Every day at a scheduled time (currently 6:00 PM Singapore Time), this tool automatically:
1. Takes a full inventory of resources in our Azure environment
2. Compares it to yesterday's inventory
3. Identifies what was **added**, **removed**, or **modified**
4. Draws a visual diagram showing how resources relate to each other (e.g. a virtual machine connected to its network card, disk, and virtual network)
5. Saves the diagram and a change report into our own Azure storage account

**In one sentence:** it's an automated daily "what changed in our Azure environment, and what does it look like" report — with zero manual effort once set up.

---

## 2. Why we built this

- **Visibility:** without this, confirming what changed in production requires manually checking the Azure Portal or running ad-hoc queries.
- **Audit trail:** every day's snapshot is kept permanently, giving a historical record of the environment over time — useful for troubleshooting, compliance, or change verification.
- **No manual effort:** runs on its own schedule; nobody needs to log in and check anything unless they want to review the output.
- **Safe by design:** it only *reads* our environment and *writes* to a storage location we control — it cannot accidentally change or delete anything in production (explained further in Section 5).

---

## 3. How it works (high-level flow)

```
 Every day at 6:00 PM SGT
          │
          ▼
 ┌─────────────────────────┐
 │ 1. Query Azure for every  │   READ-ONLY
 │    resource's details      │
 └───────────┬─────────────┘
             │
             ▼
 ┌─────────────────────────┐
 │ 2. Save today's inventory │   WRITE (to our storage account only)
 └───────────┬─────────────┘
             │
             ▼
 ┌─────────────────────────┐
 │ 3. Compare to yesterday's │   No Azure calls — pure calculation
 │    inventory               │
 └───────────┬─────────────┘
             │
             ▼
 ┌─────────────────────────┐
 │ 4. Draw a diagram + table  │   No Azure calls — pure calculation
 │    of what changed          │
 └───────────┬─────────────┘
             │
             ▼
 ┌─────────────────────────┐
 │ 5. Save the diagram +      │   WRITE (to our storage account only)
 │    report                  │
 └─────────────────────────┘
```

---

## 4. Tools and technology used

| Tool / Service | What it's for | Why we chose it |
|---|---|---|
| **Azure Resource Graph** | The Azure service that lets us query "show me every resource and its details" quickly, across the whole environment | Fastest, most complete way to inventory resources — far quicker than checking each resource type individually |
| **Azure Blob Storage** | Where the daily snapshots, change reports, and diagrams get saved | Simple, cheap, and we already have a storage account in use |
| **Azure Functions** | The compute service that runs our code automatically on a timer, without needing a server that's always on | "Serverless" — we only pay for the few seconds per day it actually runs, and there's no server to patch or maintain |
| **Python** | The programming language the logic is written in | Well-suited for this kind of data processing, and has mature libraries for talking to Azure and generating images |
| **Managed Identity** | An Azure-managed "credential" tied to our Function App, used instead of a stored password/key | No secrets stored anywhere in the code — Azure handles authentication securely on its own |
| **Microsoft's official Azure icon set** | The visual icons used in the diagram (VM, Virtual Network, etc.) | So the diagram looks consistent with what people already recognize from the Azure Portal |

---

## 5. Where it reads vs. where it writes (the safety boundary)

This is the most important section for understanding risk:

| Action | Read or Write? | Scope |
|---|---|---|
| Querying resource details via Resource Graph | **Read only** | Can see resource names, types, configurations — **cannot** create, modify, or delete anything |
| Saving snapshot/diff/diagram files | **Write** | Limited to specific containers inside **one storage account we designated** — nothing else |

**What it can never do, by design:**
- Create, modify, or delete any Azure resource (VM, network, storage account, etc.)
- Create a resource group, subscription-level setting, or any infrastructure
- Write to any storage location other than the specific containers we set up for it

The only permission granted to this tool's identity is:
- **Reader** role on the subscription (view-only)
- **Storage Blob Data Contributor** role, scoped to just the one storage account it writes to

This is a deliberate "least privilege" design — the tool literally does not have the technical ability to change production resources, even if the code had a bug.

---

## 6. Why Azure Functions specifically (not a VM, not a manual script)

| Option | Downside |
|---|---|
| Running it manually on someone's laptop | Requires a person to remember and run it daily; stops working if that laptop is off/unavailable |
| A dedicated always-on virtual machine | Costs money 24/7 even though the job only runs for a few seconds once a day; requires patching/maintenance |
| **Azure Functions (what we chose)** | Runs automatically on schedule, costs effectively **$0/month** at this usage level (well within Azure's free monthly allowance), no server to maintain |

---

## 7. Prerequisites and dependencies

**Already required to be in place before this can run:**
- An Azure subscription with resources in it (the thing being monitored)
- A storage account with three folders ("containers") already created: for snapshots, change reports, and diagrams
- Someone with permission to create a Function App and assign it the two roles mentioned in Section 5 (a one-time setup step)

**Tools needed on a developer's machine to set this up or make changes** (not needed for it to keep running day-to-day):
- Azure CLI (`az`) — command-line tool for managing Azure resources
- Azure Functions Core Tools (`func`) — command-line tool for deploying code to a Function App
- Python 3.11
- VS Code or any code editor

**Once deployed, none of the above are needed for the tool to keep running** — it's fully automated from that point on.

---

## 8. Cost

| Component | Monthly cost |
|---|---|
| Azure Functions (Consumption plan) | ~$0 — covered by Azure's free monthly execution allowance at this usage (once per day) |
| Storage (a few KB/day of JSON + image files) | Negligible — well under $1/month |

---

## 9. What management should know for oversight/approval purposes

- The tool has **no ability to modify production** — its permissions are read-only against resources, write-only to our own reporting storage.
- It requires **one-time setup** (creating the Function App, granting two specific roles) — after that, it's fully automatic.
- The code and every configuration value is documented, including a step-by-step deployment runbook, so it can be reproduced, audited, or handed off to another team member if needed.
- It uses only **official Microsoft-provided components** (Azure services, official icon assets) — no third-party or unvetted tools involved.
