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
    y_by_id = {r["id"]: r for r in yesterday}
    t_by_id = {r["id"]: r for r in today}

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
            if before.get(f) != after.get(f):
                changes[f] = {"before": before.get(f), "after": after.get(f)}
        if changes:
            modified.append({"id": rid, "name": after.get("name"), "changes": changes})

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


def _extract_edges(resources: list[dict]) -> list[tuple[str, str]]:
    """
    Parse each resource's `properties` for references to other resources
    (VM -> NIC/Disk, NIC -> VNet/PublicIP/NSG) and return a list of
    (source_id, target_id) edges. Best-effort: unknown/absent properties
    are simply skipped, never raise.
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
                    edges.append((r["id"], target))
            disk = (((props.get("storageProfile", {}) or {}).get("osDisk", {}) or {})
                    .get("managedDisk", {}) or {}).get("id")
            target = resolve(disk)
            if target:
                edges.append((r["id"], target))

        elif rtype == "microsoft.network/networkinterfaces":
            for ipconf in props.get("ipConfigurations", []) or []:
                ipprops = ipconf.get("properties", {}) or {}
                subnet_id = (ipprops.get("subnet", {}) or {}).get("id")
                vnet_target = vnet_id_from_subnet_id(subnet_id)
                if vnet_target:
                    edges.append((r["id"], vnet_target))
                pip_target = resolve((ipprops.get("publicIPAddress", {}) or {}).get("id"))
                if pip_target:
                    edges.append((r["id"], pip_target))
            nsg_target = resolve((props.get("networkSecurityGroup", {}) or {}).get("id"))
            if nsg_target:
                edges.append((r["id"], nsg_target))

        elif rtype == "microsoft.web/sites":
            # App Service -> its App Service Plan
            plan_target = resolve(props.get("serverFarmId"))
            if plan_target:
                edges.append((r["id"], plan_target))
            # App Service -> its Application Insights component, via Azure's
            # own "hidden-link:<resource-id>" tag convention (the same
            # mechanism the Azure Portal itself uses to draw this link).
            for tag_key, tag_val in (r.get("tags") or {}).items():
                if tag_key.lower().startswith("hidden-link:") and tag_val == "Resource":
                    linked_id = tag_key.split("hidden-link:", 1)[-1]
                    target = resolve(linked_id)
                    if target:
                        edges.append((r["id"], target))

        elif rtype == "microsoft.insights/components":
            # Application Insights -> its backing Log Analytics Workspace
            workspace_target = resolve(props.get("WorkspaceResourceId"))
            if workspace_target:
                edges.append((r["id"], workspace_target))

    return edges


def generate_diagram_svg(diff: dict, today_resources: list[dict], date_str: str) -> str:
    """
    Dependency-graph style SVG diagram matching Azure's Resource Visualizer:
    each resource is an icon card, connected to related resources by arrows
    (VM -> NIC/Disk, NIC -> VNet/PublicIP/NSG). Cards are color-coded by
    change status (green border=added, red=removed, amber=modified).
    Below the graph, an Added/Removed table lists changes in text form.
    """

    def esc(s):
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    modified_ids = {m["id"] for m in diff["modified"]}
    added_ids = {r["id"] for r in diff["added"]}
    removed = diff["removed"]

    def status_of(rid):
        if rid in added_ids:
            return "added"
        if rid in modified_ids:
            return "modified"
        return "unchanged"

    nodes = {r["id"]: r for r in today_resources}
    for r in removed:
        nodes.setdefault(r["id"], r)  # ensure removed resources still render as nodes

    edges = _extract_edges(today_resources)

    # ---- Tiered layout: isolated nodes on top row, then chains by depth ----
    incoming = {rid: set() for rid in nodes}
    outgoing = {rid: set() for rid in nodes}
    for src, tgt in edges:
        if src in nodes and tgt in nodes:
            outgoing[src].add(tgt)
            incoming[tgt].add(src)

    depth = {}

    def compute_depth(rid, seen=None):
        if rid in depth:
            return depth[rid]
        seen = seen or set()
        if rid in seen:
            return 0
        seen.add(rid)
        parents = incoming.get(rid, set())
        if not parents:
            depth[rid] = 0
        else:
            depth[rid] = 1 + max(compute_depth(p, seen) for p in parents)
        return depth[rid]

    for rid in nodes:
        compute_depth(rid)

    tiers: dict[int, list[str]] = {}
    for rid, d in depth.items():
        tiers.setdefault(d, []).append(rid)
    for rid in removed:
        tiers.setdefault(0, [])
        if rid["id"] not in depth:
            tiers[0].append(rid["id"])

    CARD_W, CARD_H = 150, 90
    GAP_X, GAP_Y = 40, 70
    PAD = 30

    positions = {}
    max_row_width = 0
    y = PAD + 10
    for tier_idx in sorted(tiers.keys()):
        row = sorted(tiers[tier_idx])
        row_width = len(row) * CARD_W + (len(row) - 1) * GAP_X
        max_row_width = max(max_row_width, row_width)
        x = PAD
        for rid in row:
            positions[rid] = (x, y)
            x += CARD_W + GAP_X
        y += CARD_H + GAP_Y

    graph_width = max_row_width + PAD * 2
    graph_height = y

    svg = [
        f'<defs><marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" '
        f'markerHeight="6" orient="auto-start-reverse">'
        f'<path d="M2 1L8 5L2 9" fill="none" stroke="#57606a" stroke-width="1.5"/></marker></defs>',
    ]

    # Edges (drawn first, under the nodes)
    for src, tgt in edges:
        if src in positions and tgt in positions:
            sx, sy = positions[src]
            tx, ty = positions[tgt]
            x1, y1 = sx + CARD_W / 2, sy + CARD_H
            x2, y2 = tx + CARD_W / 2, ty
            svg.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
                       f'stroke="#57606a" stroke-width="1" marker-end="url(#arrow)"/>')

    # Nodes
    for rid, (x, y0) in positions.items():
        r = nodes[rid]
        status = "removed" if rid in {rr["id"] for rr in removed} else status_of(rid)
        colors = STATUS_COLORS[status]
        rtype = (r.get("type") or "").lower()
        style = _get_style(rtype)

        border_w = "2.5" if status != "unchanged" else "1"
        svg.append(f'<rect x="{x}" y="{y0}" width="{CARD_W}" height="{CARD_H}" rx="8" '
                   f'fill="{colors["fill"]}" stroke="{colors["border"]}" stroke-width="{border_w}"/>')
        # Icon: official Azure icon image if the file exists, else a colored glyph circle
        cx, cy = x + CARD_W / 2, y0 + 26
        icon_uri = _load_icon_data_uri(rtype)
        if icon_uri:
            icon_size = 32
            svg.append(f'<image x="{cx - icon_size/2}" y="{cy - icon_size/2}" '
                       f'width="{icon_size}" height="{icon_size}" href="{icon_uri}"/>')
        else:
            svg.append(f'<circle cx="{cx}" cy="{cy}" r="18" fill="{style["color"]}"/>')
            svg.append(f'<text x="{cx}" y="{cy+4}" font-size="10" font-weight="bold" fill="#ffffff" '
                       f'text-anchor="middle">{style["glyph"]}</text>')
        # Name + type
        name = esc(r.get("name", "?"))
        if len(name) > 20:
            name = name[:18] + "…"
        svg.append(f'<text x="{cx}" y="{y0+58}" font-size="11" text-anchor="middle" '
                   f'fill="{colors["text"]}">{name}</text>')
        short_type = esc((r.get("type") or "").split("/")[-1])
        svg.append(f'<text x="{cx}" y="{y0+72}" font-size="9" text-anchor="middle" '
                   f'fill="#57606a">{short_type}</text>')

    # ---- 3-column table: Current Network Resources | Newly Added | Newly Deleted ----
    added = diff["added"]

    def is_network(r):
        return (r.get("type") or "").lower().startswith("microsoft.network/")

    current_network = sorted(
        [r for r in today_resources if is_network(r)],
        key=lambda r: r.get("name", "")
    )

    table_y = graph_height + 30
    row_h = 30
    col_w = graph_width / 3

    svg.append(f'<text x="{PAD}" y="{table_y}" font-size="14" font-weight="bold" '
               f'fill="#1f2328">Network Resource Changes — {esc(date_str)}</text>')
    table_y += 22

    col_headers = [
        (f"Current Network Resources ({len(current_network)})", "#1f2328"),
        (f"Newly Added ({len(added)})", "#1a7f37"),
        (f"Newly Deleted ({len(removed)})", "#cf222e"),
    ]
    for i, (label, color) in enumerate(col_headers):
        svg.append(f'<text x="{PAD + i*col_w}" y="{table_y}" font-size="12" '
                   f'font-weight="bold" fill="{color}">{esc(label)}</text>')
    table_y += 8
    svg.append(f'<line x1="{PAD}" y1="{table_y}" x2="{PAD+graph_width}" y2="{table_y}" '
               f'stroke="#d0d7de" stroke-width="1"/>')
    table_y += 16

    columns_data = [current_network, added, removed]
    max_rows = max(len(c) for c in columns_data) if any(columns_data) else 1

    for i in range(max_rows):
        ry = table_y + i * row_h
        for col_idx, col_items in enumerate(columns_data):
            if i < len(col_items):
                item = col_items[i]
                x = PAD + col_idx * col_w
                type_short = esc((item.get("type") or "").split("/")[-1])
                svg.append(f'<text x="{x}" y="{ry}" font-size="10.5" fill="#1f2328">'
                           f'{esc(item.get("name",""))} '
                           f'<tspan fill="#57606a">({type_short})</tspan></text>')
                # "Changed by" only applies to Added/Removed columns (index 1, 2) —
                # the Current Network Resources column (index 0) has no actor data.
                if col_idx in (1, 2):
                    who = esc(item.get("changed_by", "Unknown"))
                    svg.append(f'<text x="{x}" y="{ry+13}" font-size="9" fill="#8c959f">by {who}</text>')

    total_height = table_y + max_rows * row_h + 20
    total_width = max(graph_width, 700)

    header = (f'<svg viewBox="0 0 {total_width} {total_height}" xmlns="http://www.w3.org/2000/svg" '
              f'font-family="Segoe UI, Arial, sans-serif">'
              f'<rect width="{total_width}" height="{total_height}" fill="#ffffff"/>')
    return header + "\n".join(svg) + "</svg>"


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
