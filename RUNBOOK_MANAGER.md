# Runbook (Manager Overview) — Azure Resource Diff & Diagram Tool

**Purpose of this document:** explain how the tool works and how to verify it's working correctly — using only the environment's **existing** resources. This document contains **no commands that create or delete any Azure resource**. For full technical setup/deployment instructions, see the separate Technical/Test Runbook.

---

## 1. Tools and applications used by this project

| Tool | Purpose | Download |
|---|---|---|
| **Azure subscription** | Where the environment being monitored, and the tool itself, both run | (existing organizational access) |
| **Azure CLI** | Command-line tool used to set up and check on Azure resources | https://learn.microsoft.com/en-us/cli/azure/install-azure-cli |
| **Azure Functions Core Tools** | Used to deploy the tool's code to Azure | https://go.microsoft.com/fwlink/?linkid=2174087 |
| **Python 3.11** | The programming language the tool's logic is written in | https://www.python.org/downloads/ |
| **Visual Studio Code** (optional, for viewing/editing code) | Code editor | https://code.visualstudio.com/download |
| **Git for Windows** (includes Git Bash) | Provides a command-line terminal compatible with the setup steps | https://git-scm.com/download/win |
| **Microsoft Azure Architecture Icons** | Official icon artwork used in the generated diagrams | https://learn.microsoft.com/en-us/azure/architecture/icons/ |

**Note:** all of the above are free, officially provided by Microsoft (or open-source, in Git/VS Code's case) — no paid licenses or third-party tools are involved. None of these need to be installed by everyday users of the tool's output; they're only needed by whoever sets up or maintains the tool itself.

## 2. What this tool does

Every day at a scheduled time (currently **midnight Singapore Time**), the tool automatically:
1. Reads the full list of resources in our Azure environment
2. Compares it to yesterday's list
3. Identifies what was **added**, **removed**, or **modified**
4. Draws a visual diagram showing how resources relate to each other (e.g. a virtual machine connected to its network card, disk, and virtual network) — using the same official Microsoft icons seen in the Azure Portal
5. Saves the diagram and a change report into our own storage account

---

## 3. How it works (flow)

```
 Every day at 12:00 AM Singapore Time
          │
          ▼
 ┌─────────────────────────┐
 │ 1. Query Azure for every  │   READ-ONLY — cannot modify anything
 │    resource's details      │
 └───────────┬─────────────┘
             │
             ▼
 ┌─────────────────────────┐
 │ 2. Save today's inventory │   WRITE — only to our own storage account
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
 │ 5. Save the diagram +      │   WRITE — only to our own storage account
 │    report                  │
 └─────────────────────────┘
```

---

## 4. The safety boundary — where it reads vs. writes

| Action | Read or Write? | Scope |
|---|---|---|
| Querying resource details | **Read only** | Can see resource names, types, configurations — **cannot** create, modify, or delete anything |
| Saving snapshot/diff/diagram files | **Write** | Limited to specific folders inside **one storage account we designated** — nothing else |

The tool's only two permissions are:
- **Reader** on the subscription (view-only)
- **Storage write access**, scoped to just the one storage account it saves reports to

**Who these permissions belong to:** the Function App itself — the automated compute component that runs the tool's code — not any individual person's account. Azure manages this identity automatically; no one logs in as it, and no password exists for it. This is what lets the tool run unattended, on its own schedule, without any person's credentials being involved.

It has no technical ability to change production resources, even if there were a bug in the code.

---

## 5. What the output looks like

**The diagram** shows every resource as an icon (matching the Azure Portal's own icon style), connected by arrows showing real relationships — for example: a Virtual Machine connects to its Network Interface and Disk; the Network Interface connects to its Virtual Network, Public IP, and Network Security Group; a Function App connects to its App Service Plan and Application Insights.

Resources are color-coded by change status:
- **Green border** = added since yesterday
- **Red border** = removed since yesterday
- **Amber border** = modified since yesterday
- **Plain border** = unchanged

**Below the diagram**, a three-column table specifically for network-related resources:
- **Current Network Resources** — everything that exists today
- **Newly Added** — anything new since yesterday
- **Newly Deleted** — anything removed since yesterday

### Where this output is stored — three separate folders, each with a distinct purpose

| Folder (container) | Contains | Why it's kept separate |
|---|---|---|
| **snapshots** | The full raw list of every resource, saved once per day | The underlying data everything else is calculated from — kept apart so it can be retained long-term for audit purposes without cluttering the visual outputs |
| **diffs** | A plain-text/JSON report of what changed | The "what changed" answer on its own — quick to read without opening the diagram |
| **diagrams** | The visual diagram people actually look at | Kept separate so only the most recent 1-2 are retained (older ones are automatically cleaned up), independent of how long the raw data/reports are kept |

Keeping these apart also means different retention rules can apply to each — for example, diagrams are automatically limited to the 2 most recent, while the underlying data can be kept indefinitely for historical reference.

---

## 6. How to verify it's working — using only existing resources

These steps **observe already-generated output** or **trigger the existing, already-deployed automation to run early** — nothing here creates, modifies, or deletes any Azure resource.

### Check that today's report was generated
In the Azure Portal:
1. Navigate to the storage account being used for reports
2. Open the **diagrams** container
3. Confirm there's a file dated today, with a recent "Last modified" time

### View the diagram
1. Click the file, then **Download** (or open the **View/Edit** option if available)
2. Open the downloaded file in a web browser — it renders as an image directly

### View the change report (plain-text summary)
1. In the same storage account, open the **diffs** container
2. Download and open the `.md` file for today's date — it lists what was added/removed in plain, readable text

### Trigger the automation to run right now, instead of waiting for the schedule
*(This runs the existing, already-deployed function — it does not create anything new.)*
1. In the Azure Portal, navigate to the existing Function App
2. Under **Functions**, click the deployed function
3. Click **Test/Run** (if available in this Portal view) → **Run**
4. Wait about 20-30 seconds, then re-check the **diagrams**/**diffs** containers for a newer timestamp

### Confirm the schedule is set correctly
1. In the Function App's function, check its trigger configuration
2. Confirm it shows the expected daily schedule time

---

## 7. What to look for as evidence the tool is working correctly

- A new file appears in **diagrams** and **diffs** every day, automatically, without anyone manually running anything
- The diagram visually matches the current state of the environment (resources you know exist show up; resources you know were removed show up as "Removed" the day after)
- The 3-column table's "Current Network Resources" count matches expectations for the environment's actual network footprint

---

## 8. Who to contact for changes

Any change to **what** the tool tracks, **how often** it runs, or **where** it saves output requires editing the tool's code and redeploying — this is a controlled, documented process (see the Technical/Test Runbook) performed by whoever manages this tool, not something adjustable from the Portal alone.
