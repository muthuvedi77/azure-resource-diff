"""
Standalone runner — same logic as the Azure Function, but runnable directly:

    python run_local.py

Useful for:
  - Local testing before deploying
  - Running from a VM/Automation Runbook/DevOps pipeline instead of Functions

Requires the same environment variables as the Function App (see
local.settings.json.example), set in your shell or a .env file.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "snapshot_function"))

from snapshot_function import (  # noqa: E402
    get_resource_graph_client,
    get_blob_service_client,
    get_container,
    query_all_resources,
    save_snapshot,
    load_snapshot,
    diff_snapshots,
    save_diff_report,
    generate_diagram_svg,
    save_diagram,
    cleanup_old_blobs,
    CONTAINER_SNAPSHOTS,
    CONTAINER_DIFFS,
    CONTAINER_DIAGRAMS,
)
import datetime as dt
import logging

logging.basicConfig(level=logging.INFO)


def main():
    today = dt.date.today()
    yesterday = today - dt.timedelta(days=1)
    today_str, yesterday_str = today.isoformat(), yesterday.isoformat()

    blob_service = get_blob_service_client()
    snap_container = get_container(blob_service, CONTAINER_SNAPSHOTS)
    diff_container = get_container(blob_service, CONTAINER_DIFFS)
    diagram_container = get_container(blob_service, CONTAINER_DIAGRAMS)

    rg_client = get_resource_graph_client()
    today_resources = query_all_resources(rg_client)
    save_snapshot(snap_container, today_str, today_resources)

    yesterday_resources = load_snapshot(snap_container, yesterday_str)
    if yesterday_resources is None:
        print(f"No snapshot for {yesterday_str} yet — run again tomorrow to see a diff.")
        return

    diff = diff_snapshots(yesterday_resources, today_resources)
    save_diff_report(diff_container, today_str, diff)

    svg = generate_diagram_svg(diff, today_resources, today_str)
    save_diagram(diagram_container, today_str, svg)

    # Retention: keep only the 2 most recent diagrams (today + yesterday)
    cleanup_old_blobs(diagram_container, keep=2, suffix="-diagram.svg")

    print(f"Done. +{diff['summary']['added_count']} / "
          f"-{diff['summary']['removed_count']} / "
          f"~{diff['summary']['modified_count']}")


if __name__ == "__main__":
    main()
