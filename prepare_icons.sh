#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# prepare_icons.sh — bulk-copies the icon files this project needs from a
# downloaded Microsoft "Azure_Public_Service_Icons" pack into
# snapshot_function/icons/, using the correct simplified filenames the code
# expects. Reports which ones were found vs. missing (missing ones just fall
# back to the colored-glyph rendering — nothing breaks either way).
#
# Usage:
#   ./prepare_icons.sh "/c/Users/hp/Downloads/Azure_Public_Service_Icons_V24/Azure_Public_Service_Icons"
# ---------------------------------------------------------------------------

set -uo pipefail

ICONPACK="${1:?Usage: ./prepare_icons.sh /path/to/Azure_Public_Service_Icons}"
DEST="$(dirname "$0")/snapshot_function/icons"
mkdir -p "$DEST"

# search_term -> destination filename. search_term is matched case-insensitively
# against "*-icon-service-<search_term>*.svg" or "*<search_term>*.svg" anywhere
# in the pack, EXCLUDING any "(Classic)" variants.
declare -A ICON_MAP=(
  ["Virtual-Machine"]="virtual-machine.svg"
  ["Disks"]="disk.svg"
  ["Availability-Sets"]="availability-set.svg"
  ["VM-Scale-Sets"]="vm-scale-set.svg"
  ["Network-Interfaces"]="network-interface.svg"
  ["Virtual-Networks"]="virtual-network.svg"
  ["Public-IP-Addresses"]="public-ip-address.svg"
  ["Network-Security-Groups"]="network-security-group.svg"
  ["Network-Watcher"]="network-watcher.svg"
  ["Application-Gateways"]="application-gateway.svg"
  ["Firewalls"]="firewall.svg"
  ["Load-Balancers"]="load-balancer.svg"
  ["Route-Tables"]="route-table.svg"
  ["Bastions"]="bastion.svg"
  ["Virtual-Network-Gateways"]="vpn-gateway.svg"
  ["DNS-Zones"]="dns-zone.svg"
  ["Traffic-Manager-Profiles"]="traffic-manager.svg"
  ["Front-Doors"]="front-door.svg"
  ["CDN-Profiles"]="cdn-profile.svg"
  ["Storage-Accounts"]="storage-account.svg"
  ["Resource-Groups"]="resource-group.svg"
  ["App-Services"]="app-service.svg"
  ["App-Service-Plans"]="app-service-plan.svg"
  ["App-Service-Static-Apps"]="static-web-app.svg"
  ["Application-Insights"]="application-insights.svg"
  ["Action-Groups"]="action-group.svg"
  ["Log-Analytics-Workspaces"]="log-analytics-workspace.svg"
  ["Key-Vaults"]="key-vault.svg"
  ["SQL-Server"]="sql-server.svg"
  ["SQL-Database"]="sql-database.svg"
  ["Azure-Cosmos-DB"]="cosmos-db.svg"
  ["Cache-Redis"]="redis-cache.svg"
  ["Kubernetes-Services"]="kubernetes-service.svg"
  ["Container-Registries"]="container-registry.svg"
  ["Container-Instances"]="container-instance.svg"
  ["Service-Bus"]="service-bus.svg"
  ["Event-Hubs"]="event-hub.svg"
  ["Event-Grid-Topics"]="event-grid-topic.svg"
  ["Logic-Apps"]="logic-app.svg"
  ["API-Management-Services"]="api-management.svg"
  ["Automation-Accounts"]="automation-account.svg"
  ["Recovery-Services-Vaults"]="recovery-services-vault.svg"
  ["Data-Factories"]="data-factory.svg"
  ["Batch-Accounts"]="batch-account.svg"
  ["Managed-Identities"]="managed-identity.svg"
)

found=0
missing=0
missing_list=()

for term in "${!ICON_MAP[@]}"; do
  dest_name="${ICON_MAP[$term]}"

  # Find candidates: match the term, case-insensitive, exclude Classic variants
  match=$(find "$ICONPACK" -iname "*${term}*.svg" 2>/dev/null | grep -vi "classic" | head -n 1)

  if [ -n "$match" ]; then
    cp "$match" "$DEST/$dest_name"
    echo "✓ $dest_name  <-  $(basename "$match")"
    found=$((found+1))
  else
    echo "✗ MISSING: $dest_name (searched for '*${term}*.svg')"
    missing=$((missing+1))
    missing_list+=("$dest_name")
  fi
done

echo ""
echo "=========================================="
echo "Done. Found: $found   Missing: $missing"
echo "=========================================="
if [ $missing -gt 0 ]; then
  echo ""
  echo "Missing icons will fall back to colored-glyph circles (no error, just less official-looking)."
  echo "Missing:"
  for m in "${missing_list[@]}"; do
    echo "  - $m"
  done
fi
