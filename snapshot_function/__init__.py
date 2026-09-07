"""
Azure Function (Timer Trigger) — Daily Resource Snapshot, Diff & Diagram

What this does, every run:
  1. Queries Azure Resource Graph for every resource across the target scope
  2. Saves the result as a dated JSON snapshot in Blob Storage
  3. Loads yesterday's snapshot (if present) and diffs it against today's
  4. Writes a diff report (JSON + Markdown) to Blob Storage
  5. Generates an SVG diagram highlighting added (green) / removed (red) /
     modified (amber) resources, grouped by resource group
  6. Uploads the diagram to Blob Storage

Trigger: runs once daily (default 06:00 UTC — edit function.json to change).
"""

import os
import json
import logging
import base64
import datetime as dt

import requests
import azure.functions as func
from azure.identity import DefaultAzureCredential
from azure.mgmt.resourcegraph import ResourceGraphClient
from azure.mgmt.resourcegraph.models import QueryRequest
from azure.mgmt.monitor import MonitorManagementClient
from azure.storage.blob import BlobServiceClient, generate_blob_sas, BlobSasPermissions

# --------------------------------------------------------------------------
# Configuration (all via Function App settings / environment variables)
# --------------------------------------------------------------------------
STORAGE_CONNECTION_STRING = os.environ["STORAGE_CONNECTION_STRING"]
CONTAINER_SNAPSHOTS = os.environ.get("CONTAINER_SNAPSHOTS", "snapshots")
CONTAINER_DIFFS = os.environ.get("CONTAINER_DIFFS", "diffs")
CONTAINER_DIAGRAMS = os.environ.get("CONTAINER_DIAGRAMS", "diagrams")

# Optional: Slack Incoming Webhook URL. If not set, Slack notifications are
# simply skipped — everything else still works normally.
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "").strip()

# Comma-separated list of subscription IDs to scan. Leave blank to let
# Resource Graph use every subscription the identity can see.
SUBSCRIPTION_IDS = [
    s.strip() for s in os.environ.get("SUBSCRIPTION_IDS", "").split(",") if s.strip()
]

RESOURCE_GRAPH_QUERY = """
Resources
| project id, name, type, resourceGroup, location, subscriptionId, tags, kind, sku, properties
| order by id asc
"""

# --------------------------------------------------------------------------
# Azure clients
# --------------------------------------------------------------------------

def get_resource_graph_client() -> ResourceGraphClient:
    credential = DefaultAzureCredential()
    return ResourceGraphClient(credential)


def get_blob_service_client() -> BlobServiceClient:
    return BlobServiceClient.from_connection_string(STORAGE_CONNECTION_STRING)


def get_container(blob_service: BlobServiceClient, name: str):
    """
    Read-only-safe: verifies the container exists and returns a client for it.
    Does NOT create anything. Containers must already exist in the storage
    account you provide — create them yourself beforehand (e.g. via the
    Azure Portal or `az storage container create`), outside this script.
    """
    container = blob_service.get_container_client(name)
    if not container.exists():
        raise RuntimeError(
            f"Container '{name}' does not exist in this storage account. "
            f"This script does not create containers — create '{name}' "
            f"yourself first, then re-run."
        )
    return container


# --------------------------------------------------------------------------
# Step 1 — Query Resource Graph
# --------------------------------------------------------------------------

def query_all_resources(client: ResourceGraphClient) -> list[dict]:
    """Query Resource Graph, paging through all results (1000 rows/page)."""
    all_rows = []
    skip_token = None

    while True:
        request = QueryRequest(
            query=RESOURCE_GRAPH_QUERY,
            subscriptions=SUBSCRIPTION_IDS or None,
            options={"$skipToken": skip_token} if skip_token else None,
        )
        response = client.resources(request)
        all_rows.extend(response.data)

        skip_token = getattr(response, "skip_token", None)
        if not skip_token:
            break

    return all_rows


def get_monitor_client(credential, subscription_id: str) -> MonitorManagementClient:
    return MonitorManagementClient(credential, subscription_id)


def get_change_actors(
    monitor_client: MonitorManagementClient,
    subscription_id: str,
    start_time: dt.datetime,
    end_time: dt.datetime,
) -> dict[str, str]:
    """
    Queries the Azure Activity Log for Write and Delete operations within
    the given time window, and returns a mapping of
    {resource_id (lowercase): who_performed_it (email/UPN or app name)}.

    Uses the most recent matching event per resource if there were several
    in the window (e.g. a resource created then modified again same day).

    Requires only the Reader role already granted to this identity —
    Activity Log read access is included in Reader, no extra permission
    needed.
    """
    actors: dict[str, str] = {}

    filter_str = (
        f"eventTimestamp ge '{start_time.isoformat()}' "
        f"and eventTimestamp le '{end_time.isoformat()}'"
    )

    try:
        events = monitor_client.activity_logs.list(
            filter=filter_str,
            select="resourceId,caller,operationName,eventTimestamp",
        )
        for event in events:
            op_name = (event.operation_name.value or "").lower() if event.operation_name else ""
            if not (op_name.endswith("/write") or op_name.endswith("/delete")):
                continue
            if not event.resource_id or not event.caller:
                continue
            # Keep the most recent event per resource; activity_logs.list
            # generally returns newest-first, so the first match wins.
            key = event.resource_id.lower()
            if key not in actors:
                actors[key] = event.caller
    except Exception as e:
        # Never let activity log lookup failures break the main pipeline —
        # "who changed it" is a nice-to-have enrichment, not critical data.
        logging.warning("Could not fetch Activity Log data: %s", e)

    return actors


def annotate_with_actors(diff: dict, actors: dict[str, str]) -> None:
    """
    Adds a 'changed_by' field to each added/removed/modified resource in
    the diff, looked up from the actors map built by get_change_actors().
    Defaults to 'Unknown' if no matching Activity Log event was found
    (e.g. the log entry aged out, or the resource was changed by a process
    without activity logging, which is rare but possible).
    """
    for bucket_key in ("added", "removed"):
        for item in diff.get(bucket_key, []):
            item["changed_by"] = actors.get(item["id"].lower(), "Unknown")
    for item in diff.get("modified", []):
        item["changed_by"] = actors.get(item["id"].lower(), "Unknown")


# --------------------------------------------------------------------------
# Step 2 — Save snapshot
# --------------------------------------------------------------------------

def save_snapshot(container, date_str: str, resources: list[dict]) -> str:
    blob_name = f"{date_str}.json"
    payload = json.dumps(
        {"date": date_str, "resource_count": len(resources), "resources": resources},
        indent=2,
        default=str,
    )
    container.upload_blob(blob_name, payload, overwrite=True)
    logging.info("Saved snapshot %s (%d resources)", blob_name, len(resources))
    return blob_name


def load_snapshot(container, date_str: str) -> list[dict] | None:
    blob_name = f"{date_str}.json"
    blob_client = container.get_blob_client(blob_name)
    if not blob_client.exists():
        return None
    data = json.loads(blob_client.download_blob().readall())
    return data["resources"]


# --------------------------------------------------------------------------
# Step 3 — Diff two snapshots
# --------------------------------------------------------------------------

def diff_snapshots(yesterday: list[dict], today: list[dict]) -> dict:
    # Azure resource IDs are case-insensitive, and Resource Graph doesn't
    # always return the same casing for the resourceGroup segment between
    # runs (e.g. "TERRAFORMTEST" one day, "terraformtest" the next) even
    # when nothing actually changed. Matching by lowercased id keeps the
    # same physical resource from showing up as both added and removed.
    y_by_id = {r["id"].lower(): r for r in yesterday}
    t_by_id = {r["id"].lower(): r for r in today}

    added_ids = t_by_id.keys() - y_by_id.keys()
    removed_ids = y_by_id.keys() - t_by_id.keys()
    common_ids = t_by_id.keys() & y_by_id.keys()

    modified = []
    for rid in common_ids:
        before, after = y_by_id[rid], t_by_id[rid]
        # Compare the fields that matter; ignore volatile/irrelevant ones
        fields_to_check = ["location", "tags", "sku", "kind", "resourceGroup"]
        changes = {}
        for f in fields_to_check:
            before_val, after_val = before.get(f), after.get(f)
            # A pure casing difference in resourceGroup isn't a real change —
            # see the case-insensitive id matching note above.
            if (f == "resourceGroup" and isinstance(before_val, str) and isinstance(after_val, str)
                    and before_val.lower() == after_val.lower()):
                continue
            if before_val != after_val:
                changes[f] = {"before": before_val, "after": after_val}
        if changes:
            modified.append({"id": after["id"], "name": after.get("name"), "changes": changes})

    return {
        "added": [t_by_id[i] for i in added_ids],
        "removed": [y_by_id[i] for i in removed_ids],
        "modified": modified,
        "summary": {
            "added_count": len(added_ids),
            "removed_count": len(removed_ids),
            "modified_count": len(modified),
            "total_yesterday": len(yesterday),
            "total_today": len(today),
        },
    }


def save_diff_report(container, date_str: str, diff: dict) -> str:
    json_blob_name = f"{date_str}-diff.json"
    container.upload_blob(json_blob_name, json.dumps(diff, indent=2, default=str), overwrite=True)

    md_lines = [
        f"# Resource Diff Report — {date_str}",
        "",
        f"- Added: **{diff['summary']['added_count']}**",
        f"- Removed: **{diff['summary']['removed_count']}**",
        f"- Modified: **{diff['summary']['modified_count']}**",
        "",
        "## Added Resources",
    ]
    for r in diff["added"]:
        who = r.get("changed_by", "Unknown")
        md_lines.append(f"- `{r['type']}` **{r['name']}** ({r.get('resourceGroup', '-')}) — created by: {who}")

    md_lines.append("\n## Removed Resources")
    for r in diff["removed"]:
        who = r.get("changed_by", "Unknown")
        md_lines.append(f"- `{r['type']}` **{r['name']}** ({r.get('resourceGroup', '-')}) — deleted by: {who}")

    md_lines.append("\n## Modified Resources")
    for r in diff["modified"]:
        changed_fields = ", ".join(r["changes"].keys())
        who = r.get("changed_by", "Unknown")
        md_lines.append(f"- **{r['name']}** — changed: {changed_fields} — by: {who}")

    md_blob_name = f"{date_str}-diff.md"
    container.upload_blob(md_blob_name, "\n".join(md_lines), overwrite=True)

    logging.info("Saved diff report %s / %s", json_blob_name, md_blob_name)
    return json_blob_name


# --------------------------------------------------------------------------
# Step 4 — Generate SVG diagram
# --------------------------------------------------------------------------

STATUS_COLORS = {
    "added":     {"fill": "#dafbe1", "border": "#1a7f37", "text": "#1a7f37"},
    "removed":   {"fill": "#ffebe9", "border": "#cf222e", "text": "#cf222e"},
    "modified":  {"fill": "#fff8c5", "border": "#9a6700", "text": "#9a6700"},
    "unchanged": {"fill": "#ffffff", "border": "#8c959f", "text": "#1f2328"},
}

# Icon glyph + color per resource type category — used as a FALLBACK when no
# official icon file is found (see ICON_FILES below). Loosely mirrors the
# Azure portal's Resource Visualizer coloring. Explicit entries here take
# priority; anything NOT listed still gets a sensible, distinct color/glyph
# via _auto_style() below (category-colored by provider namespace) — nothing
# ever falls back to a generic gray box.
TYPE_STYLE = {
    "microsoft.compute/virtualmachines":      {"glyph": "VM",  "color": "#0078d4"},
    "microsoft.compute/disks":                {"glyph": "DISK", "color": "#106ebe"},
    "microsoft.network/networkinterfaces":    {"glyph": "NIC", "color": "#107c10"},
    "microsoft.network/virtualnetworks":      {"glyph": "VNET", "color": "#00b294"},
    "microsoft.network/publicipaddresses":    {"glyph": "IP",  "color": "#5c2d91"},
    "microsoft.network/networksecuritygroups": {"glyph": "NSG", "color": "#005a9e"},
    "microsoft.network/networkwatchers":      {"glyph": "NW",  "color": "#00bcf2"},
    "microsoft.network/applicationgateways":  {"glyph": "AGW", "color": "#8661c5"},
    "microsoft.network/azurefirewalls":       {"glyph": "FW",  "color": "#d13438"},
    "microsoft.storage/storageaccounts":      {"glyph": "ST",  "color": "#00838f"},
    "microsoft.resources/subscriptions/resourcegroups": {"glyph": "RG", "color": "#8c959f"},
    "microsoft.web/sites":                    {"glyph": "APP", "color": "#ff9d00"},
    "microsoft.web/serverfarms":              {"glyph": "PLAN", "color": "#59b4d9"},
    "microsoft.insights/components":          {"glyph": "AI",  "color": "#a259d9"},
    "microsoft.insights/actiongroups":        {"glyph": "AG",  "color": "#e3008c"},
    "microsoft.operationalinsights/workspaces": {"glyph": "LAW", "color": "#5c2d91"},
}

# Fallback coloring by provider namespace (e.g. "microsoft.compute", "microsoft.web")
# for any resource type not explicitly listed above — keeps every category
# visually distinct rather than collapsing everything unknown into plain gray.
CATEGORY_COLORS = {
    "microsoft.compute": "#0078d4",
    "microsoft.network": "#00b294",
    "microsoft.storage": "#00838f",
    "microsoft.web": "#ff9d00",
    "microsoft.insights": "#a259d9",
    "microsoft.operationalinsights": "#5c2d91",
    "microsoft.keyvault": "#0072c6",
    "microsoft.sql": "#e35b5b",
    "microsoft.dbforpostgresql": "#e35b5b",
    "microsoft.documentdb": "#e35b5b",
    "microsoft.cache": "#e35b5b",
    "microsoft.containerservice": "#326ce5",
    "microsoft.containerregistry": "#0072c6",
    "microsoft.containerinstance": "#326ce5",
    "microsoft.servicebus": "#7fba00",
    "microsoft.eventhub": "#7fba00",
    "microsoft.eventgrid": "#7fba00",
    "microsoft.logic": "#7fba00",
    "microsoft.apimanagement": "#0072c6",
    "microsoft.automation": "#68217a",
    "microsoft.recoveryservices": "#68217a",
    "microsoft.datafactory": "#0072c6",
    "microsoft.batch": "#0072c6",
    "microsoft.managedidentity": "#8c959f",
    "microsoft.cdn": "#00b294",
    "microsoft.resources": "#8c959f",
    "microsoft.alertsmanagement": "#e3008c",
}
DEFAULT_TYPE_STYLE = {"glyph": "RES", "color": "#605e5c"}


def _auto_glyph(rtype: str) -> str:
    """Derive a short 2-4 letter glyph from a resource type's last path
    segment, e.g. 'microsoft.web/serverfarms' -> 'SF', 'actiongroups' -> 'AG'."""
    last = rtype.split("/")[-1] if rtype else "res"
    # crude camelCase/plural split: take capitalized chunks if present, else
    # just use the first 3-4 letters of the raw segment.
    letters = "".join(ch for ch in last if ch.isalpha())
    if not letters:
        return "RES"
    return letters[:4].upper()


def _get_style(rtype: str) -> dict:
    """Explicit TYPE_STYLE entry if we have one; otherwise an auto-generated
    style colored by provider namespace, so every resource type is visually
    distinct — never a plain generic gray box."""
    if rtype in TYPE_STYLE:
        return TYPE_STYLE[rtype]
    namespace = rtype.split("/")[0] if "/" in rtype else rtype
    color = CATEGORY_COLORS.get(namespace, DEFAULT_TYPE_STYLE["color"])
    return {"glyph": _auto_glyph(rtype), "color": color}


# Official Microsoft Azure Architecture Icon filenames, expected to be placed
# in an `icons/` folder alongside this file (deployed with the rest of the
# code). Download the official set from:
#   https://learn.microsoft.com/en-us/azure/architecture/icons/
# Extract the SVGs you need and rename/copy them to match these filenames.
# If a file is missing, the colored-glyph fallback above is used instead —
# nothing breaks, it just looks slightly less official until the icon is added.
ICON_DIR = os.path.join(os.path.dirname(__file__), "icons")
ICON_FILES = {
    "microsoft.compute/virtualmachines":       "virtual-machine.svg",
    "microsoft.compute/disks":                 "disk.svg",
    "microsoft.compute/availabilitysets":      "availability-set.svg",
    "microsoft.compute/virtualmachinescalesets": "vm-scale-set.svg",
    "microsoft.network/networkinterfaces":     "network-interface.svg",
    "microsoft.network/virtualnetworks":       "virtual-network.svg",
    "microsoft.network/publicipaddresses":     "public-ip-address.svg",
    "microsoft.network/networksecuritygroups": "network-security-group.svg",
    "microsoft.network/networkwatchers":       "network-watcher.svg",
    "microsoft.network/applicationgateways":   "application-gateway.svg",
    "microsoft.network/azurefirewalls":        "firewall.svg",
    "microsoft.network/loadbalancers":         "load-balancer.svg",
    "microsoft.network/routetables":           "route-table.svg",
    "microsoft.network/bastionhosts":          "bastion.svg",
    "microsoft.network/virtualnetworkgateways": "vpn-gateway.svg",
    "microsoft.network/dnszones":              "dns-zone.svg",
    "microsoft.network/trafficmanagerprofiles": "traffic-manager.svg",
    "microsoft.network/frontdoors":            "front-door.svg",
    "microsoft.cdn/profiles":                  "cdn-profile.svg",
    "microsoft.storage/storageaccounts":       "storage-account.svg",
    "microsoft.resources/subscriptions/resourcegroups": "resource-group.svg",
    "microsoft.web/sites":                     "app-service.svg",
    "microsoft.web/serverfarms":               "app-service-plan.svg",
    "microsoft.web/staticsites":               "static-web-app.svg",
    "microsoft.insights/components":           "application-insights.svg",
    "microsoft.insights/actiongroups":         "action-group.svg",
    "microsoft.operationalinsights/workspaces": "log-analytics-workspace.svg",
    "microsoft.keyvault/vaults":                "key-vault.svg",
    "microsoft.sql/servers":                    "sql-server.svg",
    "microsoft.sql/servers/databases":          "sql-database.svg",
    "microsoft.documentdb/databaseaccounts":    "cosmos-db.svg",
    "microsoft.cache/redis":                    "redis-cache.svg",
    "microsoft.containerservice/managedclusters": "kubernetes-service.svg",
    "microsoft.containerregistry/registries":   "container-registry.svg",
    "microsoft.containerinstance/containergroups": "container-instance.svg",
    "microsoft.servicebus/namespaces":          "service-bus.svg",
    "microsoft.eventhub/namespaces":            "event-hub.svg",
    "microsoft.eventgrid/topics":               "event-grid-topic.svg",
    "microsoft.logic/workflows":                "logic-app.svg",
    "microsoft.apimanagement/service":          "api-management.svg",
    "microsoft.automation/automationaccounts":  "automation-account.svg",
    "microsoft.recoveryservices/vaults":        "recovery-services-vault.svg",
    "microsoft.datafactory/factories":          "data-factory.svg",
    "microsoft.batch/batchaccounts":            "batch-account.svg",
    "microsoft.managedidentity/userassignedidentities": "managed-identity.svg",
}
_ICON_CACHE: dict[str, str | None] = {}


def _load_icon_data_uri(rtype: str) -> str | None:
    """
    Returns a data: URI for the resource type's official icon file, or None
    if no matching file exists in ICON_DIR. Cached per-type so each icon file
    is only read from disk once per run, regardless of how many resources of
    that type appear in the diagram.
    """
    if rtype in _ICON_CACHE:
        return _ICON_CACHE[rtype]

    filename = ICON_FILES.get(rtype)
    if not filename:
        _ICON_CACHE[rtype] = None
        return None

    path = os.path.join(ICON_DIR, filename)
    if not os.path.isfile(path):
        _ICON_CACHE[rtype] = None
        return None

    with open(path, "rb") as f:
        raw = f.read()

    if filename.lower().endswith(".svg"):
        mime = "image/svg+xml"
    elif filename.lower().endswith(".png"):
        mime = "image/png"
    else:
        _ICON_CACHE[rtype] = None
        return None

    encoded = base64.b64encode(raw).decode("ascii")
    data_uri = f"data:{mime};base64,{encoded}"
    _ICON_CACHE[rtype] = data_uri
    return data_uri


def _extract_edges(resources: list[dict]) -> list[tuple[str, str, str]]:
    """
    Parse each resource's `properties` for references to other resources
    (VM -> NIC/Disk, NIC -> VNet/PublicIP/NSG) and return a list of
    (source_id, target_id, label) edges, where label is a short human
    description of the relationship (e.g. "attached NIC", "subnet <name>").
    Best-effort: unknown/absent properties are simply skipped, never raise.
    """
    edges = []
    by_id = {r["id"].lower(): r["id"] for r in resources if r.get("id")}

    def resolve(ref_id):
        if not ref_id:
            return None
        return by_id.get(ref_id.lower())

    def vnet_id_from_subnet_id(subnet_id):
        # .../virtualNetworks/{vnet}/subnets/{subnet} -> resolve the VNet's own id
        if not subnet_id or "/subnets/" not in subnet_id.lower():
            return None
        prefix = subnet_id.lower().split("/subnets/")[0]
        return by_id.get(prefix)

    for r in resources:
        props = r.get("properties") or {}
        rtype = (r.get("type") or "").lower()

        if rtype == "microsoft.compute/virtualmachines":
            for nic in (props.get("networkProfile", {}) or {}).get("networkInterfaces", []) or []:
                target = resolve(nic.get("id"))
                if target:
                    edges.append((r["id"], target, "attached NIC"))
            disk = (((props.get("storageProfile", {}) or {}).get("osDisk", {}) or {})
                    .get("managedDisk", {}) or {}).get("id")
            target = resolve(disk)
            if target:
                edges.append((r["id"], target, "OS disk"))

        elif rtype == "microsoft.network/networkinterfaces":
            for ipconf in props.get("ipConfigurations", []) or []:
                ipprops = ipconf.get("properties", {}) or {}
                subnet_id = (ipprops.get("subnet", {}) or {}).get("id")
                vnet_target = vnet_id_from_subnet_id(subnet_id)
                if vnet_target:
                    subnet_name = subnet_id.split("/subnets/")[-1] if subnet_id else "?"
                    edges.append((r["id"], vnet_target, f"subnet {subnet_name}"))
                pip_target = resolve((ipprops.get("publicIPAddress", {}) or {}).get("id"))
                if pip_target:
                    edges.append((r["id"], pip_target, "public IP"))
            nsg_target = resolve((props.get("networkSecurityGroup", {}) or {}).get("id"))
            if nsg_target:
                edges.append((r["id"], nsg_target, "secured by"))

        elif rtype == "microsoft.web/sites":
            # App Service -> its App Service Plan
            plan_target = resolve(props.get("serverFarmId"))
            if plan_target:
                edges.append((r["id"], plan_target, "hosted on"))
            # App Service -> its Application Insights component. Azure uses
            # two different "hidden-link" tag conventions depending on the
            # resource type: either the resource id lives in the tag KEY
            # (value == "Resource"), or — as seen on Function Apps — the tag
            # KEY is a fixed label ("hidden-link: /app-insights-resource-id")
            # and the resource id is the tag VALUE. Check both so the edge
            # doesn't silently go missing depending on which shape shows up.
            for tag_key, tag_val in (r.get("tags") or {}).items():
                key_norm = tag_key.lower().replace(" ", "")
                if not key_norm.startswith("hidden-link:"):
                    continue
                target = None
                if tag_val == "Resource":
                    target = resolve(tag_key.split("hidden-link:", 1)[-1])
                elif isinstance(tag_val, str) and tag_val.lower().startswith("/subscriptions/"):
                    target = resolve(tag_val)
                if target:
                    edges.append((r["id"], target, "monitored by"))

        elif rtype == "microsoft.insights/components":
            # Application Insights -> its backing Log Analytics Workspace
            workspace_target = resolve(props.get("WorkspaceResourceId"))
            if workspace_target:
                edges.append((r["id"], workspace_target, "logs to"))

    return edges


GROUP_HEADER_BG = "#eef3f4"
GROUP_BORDER = "#d7e0e5"
MONO_FONT = "Consolas, 'Courier New', monospace"
TEXT_MUTED = "#5d6f78"
TEXT_FAINT = "#8a99a1"
EDGE_COLOR = "#8fa0a8"
ACCENT = "#1f6f73"
ACCENT_SOFT = "#e2f0ef"

CARD_W = 162
GAP_X = 18
TIER_GAP = 46
PANEL_PAD = 18
PANEL_HEADER_H = 40
PANEL_GAP = 18
BOARD_MAX_W = 1320
TITLE_H = 30
TABLE_ROW_H = 22


def _node_extra_line(r: dict) -> str | None:
    """
    A short, type-specific detail line shown under a node's type caption —
    e.g. a VNet's subnet CIDR, a VM's size, a disk's size/SKU. Returns None
    when the resource type has no natural "one more useful fact" to show.
    """
    props = r.get("properties") or {}
    rtype = (r.get("type") or "").lower()

    if rtype == "microsoft.network/virtualnetworks":
        subnets = props.get("subnets") or []
        if subnets:
            cidr = (subnets[0].get("properties") or {}).get("addressPrefix")
            if cidr:
                return cidr
        space = (props.get("addressSpace") or {}).get("addressPrefixes") or []
        return space[0] if space else None
    if rtype == "microsoft.compute/virtualmachines":
        return (props.get("hardwareProfile") or {}).get("vmSize")
    if rtype == "microsoft.compute/disks":
        size = props.get("diskSizeGB")
        sku = (r.get("sku") or {}).get("name")
        parts = ([f"{size} GB"] if size else []) + ([sku] if sku else [])
        return " · ".join(parts) if parts else None
    if rtype == "microsoft.storage/storageaccounts":
        return (r.get("sku") or {}).get("name")
    if rtype == "microsoft.web/serverfarms":
        sku = (r.get("sku") or {}).get("name")
        return f"SKU {sku}" if sku else None
    if rtype == "microsoft.network/publicipaddresses":
        parts = [p for p in [props.get("publicIPAllocationMethod"), props.get("ipAddress")] if p]
        return " · ".join(parts) if parts else None
    return None


def _layout_group_tiers(node_ids: list[str], edges: list[tuple[str, str, str]]) -> list[list[str]]:
    """
    Depth-based tiering scoped to a single resource group's own nodes/edges,
    so unrelated resource groups never end up sharing a row (the old
    single-graph layout tiered ALL resources together, which is what made
    it look cluttered with many small, unrelated resource groups).
    """
    id_set = set(node_ids)
    incoming: dict[str, list[str]] = {rid: [] for rid in node_ids}
    for src, tgt, _label in edges:
        if src in id_set and tgt in id_set:
            incoming[tgt].append(src)

    depth: dict[str, int] = {}

    def compute_depth(rid, seen):
        if rid in depth:
            return depth[rid]
        if rid in seen:
            return 0
        seen = seen | {rid}
        parents = incoming.get(rid, [])
        depth[rid] = 0 if not parents else 1 + max(compute_depth(p, seen) for p in parents)
        return depth[rid]

    for rid in node_ids:
        compute_depth(rid, frozenset())

    tiers: dict[int, list[str]] = {}
    for rid in sorted(node_ids):
        tiers.setdefault(depth[rid], []).append(rid)
    return [tiers[k] for k in sorted(tiers.keys())]


def _card_lines(r: dict, status: str, changed_by: str | None, xlink) -> list[tuple[str, str]]:
    name = r.get("name", "?")
    if len(name) > 22:
        name = name[:20] + "…"
    lines = [("name", name), ("type", (r.get("type") or "").split("/")[-1])]

    extra = _node_extra_line(r)
    if extra:
        lines.append(("extra", extra))

    if status != "unchanged":
        pill_label = {"added": "NEW", "removed": "REMOVED", "modified": "CHANGED"}[status]
        lines.append(("pill", pill_label))
        who = changed_by or "Unknown"
        if len(who) > 26:
            who = who[:24] + "…"
        lines.append(("who", f"by {who}"))

    if xlink:
        label, target = xlink
        text = f"{label} {target.get('name','?')}"
        if len(text) > 24:
            text = text[:22] + "…"
        lines.append(("xlink", text))

    return lines


_LINE_H = {"name": 15, "type": 13, "extra": 13, "pill": 17, "who": 13, "xlink": 16}
_ICON_BLOCK_H = 14 + 30 + 8  # top pad + icon size + gap to first text line


def _card_height(lines: list[tuple[str, str]]) -> int:
    return _ICON_BLOCK_H + sum(_LINE_H[k] for k, _ in lines) + 10


def _render_card(esc, r: dict, status: str, changed_by, xlink, x: float, y: float) -> list[str]:
    lines = _card_lines(r, status, changed_by, xlink)
    h = _card_height(lines)
    colors = STATUS_COLORS[status]
    rtype = (r.get("type") or "").lower()
    style = _get_style(rtype)

    svg = []
    dash = ' stroke-dasharray="5,3"' if status == "removed" else ""
    border_w = "2" if status != "unchanged" else "1"
    svg.append(f'<rect x="{x}" y="{y}" width="{CARD_W}" height="{h}" rx="9" '
               f'fill="{colors["fill"]}" stroke="{colors["border"]}" stroke-width="{border_w}"{dash}/>')

    cx = x + CARD_W / 2
    icon_cy = y + 14 + 15
    icon_uri = _load_icon_data_uri(rtype)
    if icon_uri:
        size = 30
        svg.append(f'<image x="{cx - size/2}" y="{icon_cy - size/2}" width="{size}" height="{size}" href="{icon_uri}"/>')
    else:
        svg.append(f'<circle cx="{cx}" cy="{icon_cy}" r="15" fill="{style["color"]}"/>')
        svg.append(f'<text x="{cx}" y="{icon_cy+4}" font-size="9" font-weight="bold" fill="#ffffff" '
                   f'text-anchor="middle">{style["glyph"]}</text>')

    cursor_y = y + _ICON_BLOCK_H
    for kind, text in lines:
        cursor_y += _LINE_H[kind]
        if kind == "name":
            svg.append(f'<text x="{cx}" y="{cursor_y-3}" font-size="11.5" font-weight="600" '
                       f'text-anchor="middle" fill="{colors["text"]}">{esc(text)}</text>')
        elif kind in ("type", "extra"):
            fill = TEXT_FAINT if kind == "type" else TEXT_MUTED
            svg.append(f'<text x="{cx}" y="{cursor_y-3}" font-size="9" text-anchor="middle" '
                       f'font-family="{MONO_FONT}" fill="{fill}">{esc(text)}</text>')
        elif kind == "pill":
            pill_w = len(text) * 6 + 14
            py = cursor_y - 12
            svg.append(f'<rect x="{cx-pill_w/2}" y="{py}" width="{pill_w}" height="14" rx="7" fill="{colors["border"]}"/>')
            svg.append(f'<text x="{cx}" y="{py+10.5}" font-size="8.5" font-weight="700" text-anchor="middle" '
                       f'font-family="{MONO_FONT}" fill="#ffffff">{esc(text)}</text>')
        elif kind == "who":
            svg.append(f'<text x="{cx}" y="{cursor_y-2}" font-size="8.5" text-anchor="middle" '
                       f'font-family="{MONO_FONT}" fill="{TEXT_FAINT}">{esc(text)}</text>')
        elif kind == "xlink":
            svg.append(f'<line x1="{x+10}" y1="{cursor_y-13}" x2="{x+CARD_W-10}" y2="{cursor_y-13}" '
                       f'stroke="{GROUP_BORDER}" stroke-dasharray="2,2"/>')
            svg.append(f'<text x="{cx}" y="{cursor_y-2}" font-size="8.5" text-anchor="middle" '
                       f'font-family="{MONO_FONT}" fill="{ACCENT}">↗ {esc(text)}</text>')
    return svg


def _build_group_panel(esc, panel_idx: int, rg_name: str, group_nodes: list[dict],
                        in_edges: list[tuple[str, str, str]], status_of, changed_by_map: dict[str, str],
                        cross_out: dict[str, tuple]) -> tuple[str, float, float]:
    node_ids = [r["id"] for r in group_nodes]
    by_id = {r["id"]: r for r in group_nodes}
    tiers = _layout_group_tiers(node_ids, in_edges)

    positions: dict[str, tuple[float, float, int]] = {}
    row_widths = []
    y_cursor = float(PANEL_HEADER_H + PANEL_PAD)
    row_layout = []
    for tier_ids in tiers:
        heights = [_card_height(_card_lines(by_id[rid], status_of(rid), changed_by_map.get(rid), cross_out.get(rid)))
                   for rid in tier_ids]
        row_h = max(heights) if heights else 0
        row_w = len(tier_ids) * CARD_W + (len(tier_ids) - 1) * GAP_X
        row_widths.append(row_w)
        row_layout.append((tier_ids, y_cursor, row_h))
        y_cursor += row_h + TIER_GAP

    content_w = max(row_widths) if row_widths else CARD_W
    loc = group_nodes[0].get("location", "")
    # The header (icon + RG name + count badge + location) can need more
    # width than the card grid itself, especially for single-card panels —
    # without this the name and location text overlap in the header.
    header_content_w = (14 + 24 + len(rg_name) * 7.2 + 10 + 18 + 20
                         + len(loc) * 6.3 + 14)
    panel_w = max(content_w + 2 * PANEL_PAD, header_content_w)
    panel_h = y_cursor - TIER_GAP + PANEL_PAD
    avail_w = panel_w - 2 * PANEL_PAD

    for tier_ids, row_y, row_h in row_layout:
        row_w = len(tier_ids) * CARD_W + (len(tier_ids) - 1) * GAP_X
        x = PANEL_PAD + (avail_w - row_w) / 2
        for rid in tier_ids:
            positions[rid] = (x, row_y, row_h)
            x += CARD_W + GAP_X

    svg = [
        f'<rect x="0" y="0" width="{panel_w}" height="{panel_h}" rx="13" '
        f'fill="#ffffff" stroke="{GROUP_BORDER}" stroke-width="1"/>',
        f'<path d="M0,13 A13,13 0 0 1 13,0 L{panel_w-13},0 A13,13 0 0 1 {panel_w},13 '
        f'L{panel_w},{PANEL_HEADER_H} L0,{PANEL_HEADER_H} Z" fill="{GROUP_HEADER_BG}"/>',
        f'<line x1="0" y1="{PANEL_HEADER_H}" x2="{panel_w}" y2="{PANEL_HEADER_H}" stroke="{GROUP_BORDER}"/>',
    ]
    rg_icon = _load_icon_data_uri("microsoft.resources/subscriptions/resourcegroups")
    header_x = 14
    if rg_icon:
        svg.append(f'<image x="{header_x}" y="{PANEL_HEADER_H/2-9}" width="18" height="18" href="{rg_icon}"/>')
        header_x += 24
    svg.append(f'<text x="{header_x}" y="{PANEL_HEADER_H/2+4}" font-size="12" font-weight="600" '
               f'font-family="{MONO_FONT}" fill="#16232a">{esc(rg_name)}</text>')
    count_badge_x = header_x + len(rg_name) * 7.2 + 10
    svg.append(f'<rect x="{count_badge_x}" y="{PANEL_HEADER_H/2-8}" width="18" height="16" rx="8" fill="{ACCENT_SOFT}"/>')
    svg.append(f'<text x="{count_badge_x+9}" y="{PANEL_HEADER_H/2+4}" font-size="9.5" font-weight="600" '
               f'font-family="{MONO_FONT}" text-anchor="middle" fill="{ACCENT}">{len(group_nodes)}</text>')
    svg.append(f'<text x="{panel_w-14}" y="{PANEL_HEADER_H/2+4}" font-size="9.5" text-anchor="end" '
               f'font-family="{MONO_FONT}" fill="{TEXT_FAINT}">{esc(loc)}</text>')

    marker_id = f"arrow-{panel_idx}"
    if in_edges:
        svg.append(f'<defs><marker id="{marker_id}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" '
                   f'markerHeight="6" orient="auto-start-reverse">'
                   f'<path d="M2 1L8 5L2 9" fill="none" stroke="{EDGE_COLOR}" stroke-width="1.5"/></marker></defs>')
    for src, tgt, label in in_edges:
        if src not in positions or tgt not in positions:
            continue
        sx, sy, sh = positions[src]
        tx, ty, _th = positions[tgt]
        x1, y1 = sx + CARD_W / 2, sy + sh
        x2, y2 = tx + CARD_W / 2, ty
        svg.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{EDGE_COLOR}" '
                   f'stroke-width="1.3" marker-end="url(#{marker_id})"/>')
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        lbl_w = len(label) * 5.4 + 8
        svg.append(f'<rect x="{mx-lbl_w/2}" y="{my-7}" width="{lbl_w}" height="13" rx="3" fill="#ffffff"/>')
        svg.append(f'<text x="{mx}" y="{my+3}" font-size="9" text-anchor="middle" '
                   f'font-family="{MONO_FONT}" fill="{TEXT_MUTED}">{esc(label)}</text>')

    for rid, (x, y, _h) in positions.items():
        svg.extend(_render_card(esc, by_id[rid], status_of(rid), changed_by_map.get(rid), cross_out.get(rid), x, y))

    return "\n".join(svg), panel_w, panel_h


def _build_resource_table(esc, all_nodes: list[dict], status_of, changed_by_map: dict[str, str], width: float) -> tuple[str, float]:
    rows = sorted(all_nodes, key=lambda r: (r.get("resourceGroup", ""), r.get("name", "")))
    width = max(width, 760)
    col = {"rg": 0, "type": width * 0.28, "name": width * 0.52, "status": width * 0.78, "who": width * 0.87}

    svg = [f'<text x="0" y="16" font-size="15" font-weight="700" fill="#16232a">'
           f'All Resources in Subscription ({len(rows)})</text>']
    y = 40.0
    headers = [("Resource Group", "rg"), ("Type", "type"), ("Name", "name"), ("Status", "status"), ("Changed By", "who")]
    for text, key in headers:
        svg.append(f'<text x="{col[key]}" y="{y}" font-size="10.5" font-weight="700" '
                   f'fill="{TEXT_MUTED}">{esc(text)}</text>')
    y += 6
    svg.append(f'<line x1="0" y1="{y}" x2="{width}" y2="{y}" stroke="{GROUP_BORDER}"/>')
    y += TABLE_ROW_H

    for i, r in enumerate(rows):
        if i % 2 == 1:
            svg.append(f'<rect x="0" y="{y-15}" width="{width}" height="{TABLE_ROW_H}" fill="#f6f8f9"/>')
        status = status_of(r["id"])
        colors = STATUS_COLORS[status]
        svg.append(f'<text x="{col["rg"]}" y="{y}" font-size="10" font-family="{MONO_FONT}" '
                   f'fill="{TEXT_MUTED}">{esc(r.get("resourceGroup",""))}</text>')
        svg.append(f'<text x="{col["type"]}" y="{y}" font-size="10" font-family="{MONO_FONT}" '
                   f'fill="{TEXT_FAINT}">{esc((r.get("type") or "").split("/")[-1])}</text>')
        svg.append(f'<text x="{col["name"]}" y="{y}" font-size="10.5" fill="#16232a">{esc(r.get("name",""))}</text>')
        if status != "unchanged":
            svg.append(f'<text x="{col["status"]}" y="{y}" font-size="9.5" font-weight="700" '
                       f'font-family="{MONO_FONT}" fill="{colors["text"]}">{status.upper()}</text>')
            who = changed_by_map.get(r["id"], "Unknown")
            svg.append(f'<text x="{col["who"]}" y="{y}" font-size="9.5" font-family="{MONO_FONT}" '
                       f'fill="{TEXT_FAINT}">{esc(who)}</text>')
        y += TABLE_ROW_H

    return "\n".join(svg), y


def generate_diagram_svg(diff: dict, today_resources: list[dict], date_str: str) -> str:
    """
    Resource-group-grouped topology diagram: each resource group renders as
    its own bordered panel with a small dependency graph inside (icon cards
    + labeled arrows for real Azure relationships), packed left-to-right
    into rows. This replaced the old single-graph layout, which tiered every
    resource in the subscription together regardless of resource group and
    looked cluttered as a result. Relationships that cross resource groups
    are shown as a note on the source card instead of a line between panels.
    Below the board, a single flat table lists every resource in the
    subscription, one row each, with its change status and who made the
    change (from the Activity Log, via annotate_with_actors).
    """

    def esc(s):
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    added_ids = {r["id"] for r in diff["added"]}
    removed_ids = {r["id"] for r in diff["removed"]}
    modified_ids = {m["id"] for m in diff["modified"]}
    changed_by_map: dict[str, str] = {}
    for bucket in ("added", "removed", "modified"):
        for r in diff[bucket]:
            changed_by_map[r["id"]] = r.get("changed_by", "Unknown")

    def status_of(rid):
        if rid in removed_ids:
            return "removed"
        if rid in added_ids:
            return "added"
        if rid in modified_ids:
            return "modified"
        return "unchanged"

    nodes = {r["id"]: r for r in today_resources}
    for r in diff["removed"]:
        nodes.setdefault(r["id"], r)

    edges = _extract_edges(today_resources)

    groups: dict[str, list[dict]] = {}
    for r in nodes.values():
        groups.setdefault(r.get("resourceGroup", "?"), []).append(r)

    in_group_edges: dict[str, list[tuple[str, str, str]]] = {}
    cross_out: dict[str, tuple] = {}
    for src, tgt, label in edges:
        if src not in nodes or tgt not in nodes:
            continue
        if nodes[src].get("resourceGroup") == nodes[tgt].get("resourceGroup"):
            in_group_edges.setdefault(nodes[src]["resourceGroup"], []).append((src, tgt, label))
        else:
            cross_out.setdefault(src, (label, nodes[tgt]))

    group_order = sorted(groups.keys(), key=lambda rg: -len(groups[rg]))
    panels = []
    for idx, rg in enumerate(group_order):
        svg_str, w, h = _build_group_panel(
            esc, idx, rg, sorted(groups[rg], key=lambda r: r.get("name", "")),
            in_group_edges.get(rg, []), status_of, changed_by_map, cross_out,
        )
        panels.append({"svg": svg_str, "w": w, "h": h})

    shelves: list[list[dict]] = []
    cur_shelf: list[dict] = []
    cur_w = 0.0
    for p in panels:
        added_w = p["w"] + (PANEL_GAP if cur_shelf else 0)
        if cur_shelf and cur_w + added_w > BOARD_MAX_W:
            shelves.append(cur_shelf)
            cur_shelf, cur_w = [], 0.0
            added_w = p["w"]
        cur_shelf.append(p)
        cur_w += added_w
    if cur_shelf:
        shelves.append(cur_shelf)

    board_width = 700.0
    for shelf in shelves:
        board_width = max(board_width, sum(p["w"] for p in shelf) + PANEL_GAP * (len(shelf) - 1))

    placed = []
    y_cursor = 0.0
    for shelf in shelves:
        shelf_h = max(p["h"] for p in shelf)
        x_cursor = 0.0
        for p in shelf:
            placed.append((p, x_cursor, y_cursor))
            x_cursor += p["w"] + PANEL_GAP
        y_cursor += shelf_h + PANEL_GAP
    board_height = y_cursor - PANEL_GAP if placed else 0.0

    svg_body = [f'<text x="0" y="16" font-size="11" font-family="{MONO_FONT}" fill="{ACCENT}">'
                f'AZURE RESOURCE GRAPH · {esc(date_str)}</text>']
    svg_body += [f'<g transform="translate({bx},{by+TITLE_H})">{p["svg"]}</g>' for p, bx, by in placed]

    table_y = TITLE_H + board_height + 34
    table_svg, table_h = _build_resource_table(esc, list(nodes.values()), status_of, changed_by_map, board_width)
    svg_body.append(f'<g transform="translate(0,{table_y})">{table_svg}</g>')

    total_width = max(board_width, 760)
    total_height = table_y + table_h + 20

    header = (f'<svg viewBox="0 0 {total_width} {total_height}" xmlns="http://www.w3.org/2000/svg" '
              f'font-family="Segoe UI, Arial, sans-serif">'
              f'<rect width="{total_width}" height="{total_height}" fill="#ffffff"/>')
    return header + "\n".join(svg_body) + "</svg>"


def save_diagram(container, date_str: str, svg_content: str) -> str:
    blob_name = f"{date_str}-diagram.svg"
    container.upload_blob(
        blob_name, svg_content, overwrite=True,
        content_settings=__import__("azure.storage.blob", fromlist=["ContentSettings"])
        .ContentSettings(content_type="image/svg+xml"),
    )
    logging.info("Saved diagram %s", blob_name)
    return blob_name


def cleanup_old_blobs(container, keep: int = 2, suffix: str = "") -> None:
    """
    Retention policy: keeps only the `keep` most recent dated blobs in a
    container (matched by filename prefix, e.g. '2026-09-06'), deleting
    everything older. Blob names are expected to start with an ISO date
    (YYYY-MM-DD), which sorts correctly as plain strings — no date parsing
    needed. `suffix` can filter to a specific file type within a container
    that holds multiple file types per date (e.g. "-diff.json" vs "-diff.md").
    """
    all_names = [b.name for b in container.list_blobs()]
    if suffix:
        all_names = [n for n in all_names if n.endswith(suffix)]

    # ISO date prefixes sort correctly as plain strings (2026-09-05 < 2026-09-06)
    all_names.sort()

    if len(all_names) <= keep:
        return

    to_delete = all_names[:-keep]  # everything except the last `keep` entries
    for name in to_delete:
        container.delete_blob(name)
        logging.info("Retention cleanup: deleted old blob %s", name)


def _parse_connection_string(conn_str: str) -> dict:
    """Parses a storage connection string into its key=value parts."""
    parts = {}
    for segment in conn_str.split(";"):
        if "=" in segment:
            key, _, value = segment.partition("=")
            parts[key] = value
    return parts


def generate_sas_url(container_name: str, blob_name: str, expiry_hours: int = 168) -> str:
    """
    Generates a time-limited, read-only SAS URL for a blob, so it can be
    viewed directly (e.g. opened in a browser from a Teams message) without
    granting broader storage access. Defaults to a 7-day expiry.
    """
    conn = _parse_connection_string(STORAGE_CONNECTION_STRING)
    account_name = conn.get("AccountName")
    account_key = conn.get("AccountKey")

    sas_token = generate_blob_sas(
        account_name=account_name,
        container_name=container_name,
        blob_name=blob_name,
        account_key=account_key,
        permission=BlobSasPermissions(read=True),
        expiry=dt.datetime.utcnow() + dt.timedelta(hours=expiry_hours),
    )
    return f"https://{account_name}.blob.core.windows.net/{container_name}/{blob_name}?{sas_token}"


def send_slack_notification(diff: dict, date_str: str, diagram_url: str, diff_report_url: str) -> None:
    """
    Posts a summary message to a Slack channel via an Incoming Webhook, with
    links to view the diagram and the full diff report. Silently does
    nothing if SLACK_WEBHOOK_URL isn't configured — this feature is entirely
    optional and never blocks the rest of the run.
    """
    if not SLACK_WEBHOOK_URL:
        return

    added = diff["added"]
    removed = diff["removed"]
    modified = diff["modified"]

    def _list_names(items, limit=10):
        entries = [f"{i.get('name', '?')} _(by {i.get('changed_by', 'Unknown')})_" for i in items[:limit]]
        text = "\n".join(entries)
        if len(items) > limit:
            text += f"\n_and {len(items) - limit} more_"
        return text or "_(none)_"

    emoji = "🔴" if removed else "🟢"

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{emoji} Azure Resource Changes — {date_str}"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Added:*\n{len(added)}"},
                {"type": "mrkdwn", "text": f"*Removed:*\n{len(removed)}"},
                {"type": "mrkdwn", "text": f"*Modified:*\n{len(modified)}"},
            ],
        },
    ]

    if added:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                       "text": f"*Added resources:*\n{_list_names(added)}"}})
    if removed:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                       "text": f"*Removed resources:*\n{_list_names(removed)}"}})
    if modified:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                       "text": f"*Modified resources:*\n{_list_names(modified)}"}})

    blocks.append({
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "View Diagram"},
                "url": diagram_url,
                "style": "primary",
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "View Full Diff Report"},
                "url": diff_report_url,
            },
        ],
    })

    payload = {
        "text": f"Azure Resource Changes — {date_str} (+{len(added)} / -{len(removed)} / ~{len(modified)})",
        "blocks": blocks,
    }

    try:
        response = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=15)
        response.raise_for_status()
        logging.info("Slack notification sent successfully.")
    except requests.exceptions.RequestException as e:
        # Never let a notification failure break the actual data pipeline.
        logging.warning("Failed to send Slack notification: %s", e)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main(mytimer: func.TimerRequest) -> None:
    today = dt.date.today()
    yesterday = today - dt.timedelta(days=1)
    today_str = today.isoformat()
    yesterday_str = yesterday.isoformat()

    logging.info("Starting resource snapshot/diff run for %s", today_str)

    # --- TEMPORARY DEBUG: confirm icons folder visibility on the deployed app ---
    logging.info("DEBUG: ICON_DIR resolved to: %s", ICON_DIR)
    logging.info("DEBUG: ICON_DIR exists: %s", os.path.isdir(ICON_DIR))
    if os.path.isdir(ICON_DIR):
        logging.info("DEBUG: ICON_DIR contents: %s", os.listdir(ICON_DIR))
        for rtype, fname in ICON_FILES.items():
            fpath = os.path.join(ICON_DIR, fname)
            logging.info("DEBUG: %s -> %s exists=%s", fname, fpath, os.path.isfile(fpath))
        test_uri = _load_icon_data_uri("microsoft.compute/virtualmachines")
        logging.info("DEBUG: _load_icon_data_uri('...virtualmachines') returned: %s",
                      (test_uri[:60] + "...") if test_uri else "None")
    else:
        logging.info("DEBUG: __file__ resolved to: %s", __file__)
        logging.info("DEBUG: parent dir contents: %s", os.listdir(os.path.dirname(__file__)))
    # --- END TEMPORARY DEBUG ---

    blob_service = get_blob_service_client()
    snap_container = get_container(blob_service, CONTAINER_SNAPSHOTS)
    diff_container = get_container(blob_service, CONTAINER_DIFFS)
    diagram_container = get_container(blob_service, CONTAINER_DIAGRAMS)

    rg_client = get_resource_graph_client()
    today_resources = query_all_resources(rg_client)
    save_snapshot(snap_container, today_str, today_resources)

    yesterday_resources = load_snapshot(snap_container, yesterday_str)
    if yesterday_resources is None:
        logging.warning("No snapshot found for %s — skipping diff (first run?)", yesterday_str)
        return

    diff = diff_snapshots(yesterday_resources, today_resources)

    # Enrich the diff with "who made this change" from the Activity Log,
    # covering the window since yesterday's run. Best-effort — if this
    # fails for any reason, every resource just shows changed_by="Unknown"
    # rather than breaking the run.
    if SUBSCRIPTION_IDS:
        try:
            credential = DefaultAzureCredential()
            monitor_client = get_monitor_client(credential, SUBSCRIPTION_IDS[0])
            window_start = dt.datetime.combine(yesterday, dt.time.min)
            window_end = dt.datetime.combine(today, dt.time.max)
            actors = get_change_actors(monitor_client, SUBSCRIPTION_IDS[0], window_start, window_end)
            annotate_with_actors(diff, actors)
        except Exception as e:
            logging.warning("Skipping change-actor lookup due to error: %s", e)
            annotate_with_actors(diff, {})
    else:
        annotate_with_actors(diff, {})

    save_diff_report(diff_container, today_str, diff)

    svg = generate_diagram_svg(diff, today_resources, today_str)
    save_diagram(diagram_container, today_str, svg)

    # Retention: keep only the 2 most recent diagrams (today + yesterday)
    # so the container never grows past what's needed for manual comparison.
    cleanup_old_blobs(diagram_container, keep=2, suffix="-diagram.svg")

    # Notify Slack only if something actually changed today — no noise on
    # unchanged days. Silently skipped entirely if SLACK_WEBHOOK_URL isn't set.
    total_changes = (
        diff["summary"]["added_count"]
        + diff["summary"]["removed_count"]
        + diff["summary"]["modified_count"]
    )
    if total_changes > 0 and SLACK_WEBHOOK_URL:
        diagram_url = generate_sas_url(CONTAINER_DIAGRAMS, f"{today_str}-diagram.svg")
        diff_report_url = generate_sas_url(CONTAINER_DIFFS, f"{today_str}-diff.md")
        send_slack_notification(diff, today_str, diagram_url, diff_report_url)

    logging.info(
        "Run complete: +%d / -%d / ~%d",
        diff["summary"]["added_count"],
        diff["summary"]["removed_count"],
        diff["summary"]["modified_count"],
    )
