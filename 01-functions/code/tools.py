# ==============================================================================
# tools.py
#
# The ten Cloud Asset Inventory tools exposed over MCP, plus the registry that
# describes them to the model.
#
# Ported from the proxy-based version, with one structural change: handlers now
# take a plain `args` dict and return a plain string. They are no longer HTTP
# routes. mcp.py calls them directly in-process on tools/call, so there is no
# internal HTTP hop and no per-tool URL to secure.
#
# Responses are pre-formatted plain text on purpose — Cloud Asset Inventory
# returns deeply nested proto structs, and the model narrates a text table far
# better than it parses raw JSON.
# ==============================================================================

import json
import logging
import os
from collections import Counter

from google.cloud import asset_v1
from google.cloud import storage as gcs
from google.protobuf.json_format import MessageToDict

# ==============================================================================
# Module-level singletons
# Instantiated once per warm instance. ADC resolves to the function's service
# account at runtime — no credentials in code.
# ==============================================================================

PROJECT_ID      = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
_asset_client   = None
_storage_client = None


def _get_client() -> asset_v1.AssetServiceClient:
    global _asset_client
    if _asset_client is None:
        _asset_client = asset_v1.AssetServiceClient()
    return _asset_client


def _get_storage_client() -> gcs.Client:
    global _storage_client
    if _storage_client is None:
        _storage_client = gcs.Client()
    return _storage_client


# ==============================================================================
# Tool registry — single source of truth for what the model sees
# Served verbatim on tools/list. Unlike the proxy version there is no "route"
# key: the tool name maps straight to a Python callable in TOOL_FUNCTIONS.
# ==============================================================================

TOOL_REGISTRY = [
    {
        "name": "list_compute_instances",
        "description": (
            "Lists all Compute Engine VM instances in the project "
            "with name, machine type, zone, and status."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_storage_buckets",
        "description": (
            "Lists all Cloud Storage buckets in the project "
            "with name, location, and storage class."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "count_resources_by_type",
        "description": (
            "Returns a ranked count of all resource types deployed "
            "in the project."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "find_resources_by_label",
        "description": "Finds all resources matching a specific label key and value.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "label_key": {
                    "type": "string",
                    "description": "Label key to search for",
                },
                "label_value": {
                    "type": "string",
                    "description": "Label value to match",
                },
            },
            "required": ["label_key", "label_value"],
        },
    },
    {
        "name": "list_static_ip_addresses",
        "description": (
            "Lists all static external IP addresses in the project "
            "with name, address, region, and status."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "find_resources_by_type",
        "description": (
            "Lists all resources of a specific GCP asset type "
            "(e.g. 'compute.googleapis.com/Disk')."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "asset_type": {
                    "type": "string",
                    "description": (
                        "Full GCP asset type string, "
                        "e.g. 'compute.googleapis.com/Disk'"
                    ),
                },
            },
            "required": ["asset_type"],
        },
    },
    {
        "name": "find_resources_by_region",
        "description": (
            "Lists all resources deployed in a specific GCP region or zone "
            "(e.g. 'us-central1', 'us-central1-a')."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "region": {
                    "type": "string",
                    "description": "GCP region or zone name, e.g. 'us-central1'",
                },
            },
            "required": ["region"],
        },
    },
    {
        "name": "describe_resource",
        "description": (
            "Returns detailed information about a specific GCP resource by "
            "name or display name, including full configuration data."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "resource_name": {
                    "type": "string",
                    "description": (
                        "Display name or partial name of the resource "
                        "to look up, e.g. 'gcp-okta-mcp-func'"
                    ),
                },
            },
            "required": ["resource_name"],
        },
    },
    {
        "name": "list_cloud_functions_detail",
        "description": (
            "Lists all Cloud Functions in the project with full configuration: "
            "runtime, memory, timeout, trigger URL, service account, "
            "environment variables, and instance limits."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_bucket_objects",
        "description": (
            "Lists all objects in a specific Cloud Storage bucket "
            "with name, size, and last-modified timestamp."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "bucket_name": {
                    "type": "string",
                    "description": "Name of the Cloud Storage bucket",
                },
            },
            "required": ["bucket_name"],
        },
    },
]


# ==============================================================================
# Cloud Asset Inventory helpers
# ==============================================================================

def _list_assets(asset_types: list) -> list:
    """List assets using Cloud Asset Inventory ListAssets.

    Preferred over SearchAllResources when specific resource fields are needed
    (machineType, status) that search results do not surface.

    Args:
        asset_types: GCP asset type strings, e.g.
                     ['compute.googleapis.com/Instance'].

    Returns:
        List of Asset objects with populated resource.data fields.
    """
    client  = _get_client()
    request = asset_v1.ListAssetsRequest(
        parent       = f"projects/{PROJECT_ID}",
        asset_types  = asset_types,
        content_type = asset_v1.ContentType.RESOURCE,
    )
    return list(client.list_assets(request))


def _search_resources(query: str = "", asset_types: list = None) -> list:
    """Search resources using Cloud Asset Inventory SearchAllResources.

    Preferred over ListAssets when label or free-text filtering is needed.

    Args:
        query:       Search query string, e.g. 'labels.env:prod'.
        asset_types: Optional asset type strings to restrict results.

    Returns:
        List of ResourceSearchResult objects.
    """
    client  = _get_client()
    request = asset_v1.SearchAllResourcesRequest(
        scope       = f"projects/{PROJECT_ID}",
        query       = query,
        asset_types = asset_types or [],
    )
    return list(client.search_all_resources(request))


def _deep_convert(obj):
    """Recursively convert proto-plus collections to plain Python types.

    MapComposite and RepeatedComposite don't serialise with json.dumps — this
    walks the tree and converts them to dicts and lists.
    """
    if isinstance(obj, dict):
        return {k: _deep_convert(v) for k, v in obj.items()}
    if hasattr(obj, "items"):          # MapComposite
        return {k: _deep_convert(v) for k, v in obj.items()}
    if hasattr(obj, "__iter__") and not isinstance(obj, (str, bytes)):
        return [_deep_convert(v) for v in obj]
    return obj


def _to_dict(data) -> dict:
    """Convert resource.data to a plain Python dict.

    Proto-plus transparently unwraps google.protobuf.Struct to a MapComposite,
    which lacks the DESCRIPTOR attribute MessageToDict requires. Fall back to a
    deep recursive conversion when that happens.
    """
    try:
        return MessageToDict(data)
    except AttributeError:
        return _deep_convert(dict(data)) if data else {}


class ToolInputError(ValueError):
    """Raised when a tool is called without a required argument.

    mcp.py turns this into a JSON-RPC invalid_params error rather than a 500.
    """


# ==============================================================================
# Tool handlers
# Each takes an `args` dict (the MCP tools/call arguments) and returns a string.
# Exceptions propagate — mcp.py wraps them into a JSON-RPC error.
# ==============================================================================

def list_compute_instances(args: dict) -> str:
    """List all Compute Engine VM instances in the project."""
    assets = _list_assets(["compute.googleapis.com/Instance"])
    lines  = [f"Compute Engine instances ({len(assets)} total):", ""]
    for asset in assets:
        data   = _to_dict(asset.resource.data)
        name   = data.get("name", asset.name.split("/")[-1])
        # machineType and zone are full resource URLs — extract the suffix.
        mt     = data.get("machineType", "unknown").split("/")[-1]
        zone   = data.get("zone", "unknown").split("/")[-1]
        status = data.get("status", "UNKNOWN")
        lines.append(f"  {name:<30}  {mt:<25}  {zone:<25}  {status}")
    if not assets:
        lines.append("  (none found)")
    return "\n".join(lines)


def list_storage_buckets(args: dict) -> str:
    """List all Cloud Storage buckets in the project."""
    assets = _list_assets(["storage.googleapis.com/Bucket"])
    lines  = [f"Cloud Storage buckets ({len(assets)} total):", ""]
    for asset in assets:
        data          = _to_dict(asset.resource.data)
        name          = data.get("id", asset.name.split("/")[-1])
        location      = data.get("location", "unknown")
        storage_class = data.get("storageClass", "STANDARD")
        lines.append(f"  {name:<50}  {location:<20}  {storage_class}")
    if not assets:
        lines.append("  (none found)")
    return "\n".join(lines)


def count_resources_by_type(args: dict) -> str:
    """Return a ranked count of all resource types in the project."""
    results = _search_resources()
    counts  = Counter(r.asset_type for r in results)
    total   = sum(counts.values())
    lines   = [f"Resources by type ({total} total):", ""]
    for asset_type, count in counts.most_common():
        lines.append(f"  {count:>5}  {asset_type}")
    if not results:
        lines.append("  (none found)")
    return "\n".join(lines)


def find_resources_by_label(args: dict) -> str:
    """Find all resources matching a specific label key and value.

    Args:
        args: Must contain label_key and label_value.

    Raises:
        ToolInputError: If either argument is missing.
    """
    label_key   = str(args.get("label_key",   "")).strip()
    label_value = str(args.get("label_value", "")).strip()
    if not label_key or not label_value:
        raise ToolInputError("label_key and label_value are required")

    results = _search_resources(query=f"labels.{label_key}:{label_value}")
    lines   = [
        f"Resources with label {label_key}={label_value} "
        f"({len(results)} found):", ""
    ]
    for r in results:
        name = r.display_name or r.name.split("/")[-1]
        lines.append(f"  {name:<40}  {r.asset_type:<55}  {r.location}")
    if not results:
        lines.append("  (none found)")
    return "\n".join(lines)


def list_static_ip_addresses(args: dict) -> str:
    """List all static external IP addresses in the project."""
    assets   = _list_assets(["compute.googleapis.com/Address"])
    # EXTERNAL only — internal static IPs are RFC1918 and far less interesting
    # from a public-exposure standpoint.
    external = [
        a for a in assets
        if _to_dict(a.resource.data).get("addressType", "EXTERNAL") == "EXTERNAL"
    ]
    lines = [f"Static external IP addresses ({len(external)} total):", ""]
    for asset in external:
        data    = _to_dict(asset.resource.data)
        name    = data.get("name", asset.name.split("/")[-1])
        address = data.get("address", "(unassigned)")
        # region is a full URL for regional IPs, empty for global.
        region  = (data.get("region") or "global").split("/")[-1]
        status  = data.get("status", "UNKNOWN")
        lines.append(f"  {name:<30}  {address:<18}  {region:<20}  {status}")
    if not external:
        lines.append("  (none found)")
    return "\n".join(lines)


def find_resources_by_type(args: dict) -> str:
    """List all resources of a specific GCP asset type.

    Raises:
        ToolInputError: If asset_type is missing.
    """
    asset_type = str(args.get("asset_type", "")).strip()
    if not asset_type:
        raise ToolInputError("asset_type is required")

    results = _search_resources(asset_types=[asset_type])
    lines   = [f"Resources of type {asset_type} ({len(results)} found):", ""]
    for r in results:
        name = r.display_name or r.name.split("/")[-1]
        lines.append(f"  {name:<50}  {r.location}")
    if not results:
        lines.append("  (none found)")
    return "\n".join(lines)


def find_resources_by_region(args: dict) -> str:
    """List all resources deployed in a specific GCP region or zone.

    Raises:
        ToolInputError: If region is missing.
    """
    region = str(args.get("region", "")).strip().lower()
    if not region:
        raise ToolInputError("region is required")

    all_results = _search_resources()
    # startswith handles both region ('us-central1') and zone ('us-central1-a')
    # lookups against the same location field.
    results = [r for r in all_results if r.location.lower().startswith(region)]
    lines   = [f"Resources in {region} ({len(results)} total):", ""]
    for r in results:
        name = r.display_name or r.name.split("/")[-1]
        lines.append(f"  {name:<40}  {r.asset_type:<55}  {r.location}")
    if not results:
        lines.append(f"  (no resources found in {region})")
    return "\n".join(lines)


def describe_resource(args: dict) -> str:
    """Return detailed information about a GCP resource by name.

    Raises:
        ToolInputError: If resource_name is missing.
    """
    resource_name = str(args.get("resource_name", "")).strip()
    if not resource_name:
        raise ToolInputError("resource_name is required")

    results = _search_resources(query=resource_name)
    if not results:
        return f"No resources found matching '{resource_name}'."

    lines = [f"Found {len(results)} resource(s) matching '{resource_name}':", ""]

    for r in results:
        display = r.display_name or r.name.split("/")[-1]
        lines.append(f"  {'─' * 68}")
        lines.append(f"  Resource:  {display}")
        lines.append(f"  Full Name: {r.name}")
        lines.append(f"  Type:      {r.asset_type}")
        lines.append(f"  Location:  {r.location or 'global'}")
        if r.state:
            lines.append(f"  State:     {r.state}")
        lines.append(f"  Project:   {r.project}")
        if r.labels:
            labels = ", ".join(f"{k}={v}" for k, v in r.labels.items())
            lines.append(f"  Labels:    {labels}")
        if r.create_time:
            lines.append(f"  Created:   {r.create_time}")
        if r.update_time:
            lines.append(f"  Updated:   {r.update_time}")

        # Fetch full resource.data for this asset type and match by name. Best
        # effort — a resource type CAI can search but not list is not an error.
        try:
            full_assets = _list_assets([r.asset_type])
            for a in full_assets:
                if a.name == r.name:
                    data        = _to_dict(a.resource.data)
                    config_json = json.dumps(data, indent=4, default=str)
                    lines.append("")
                    lines.append("  Full Configuration:")
                    for cfg_line in config_json.splitlines():
                        lines.append(f"    {cfg_line}")
                    break
        except Exception:
            logging.warning("describe_resource: no detail for %s", r.asset_type)

        lines.append("")

    return "\n".join(lines)


def list_cloud_functions_detail(args: dict) -> str:
    """List all Cloud Functions with full configuration detail."""
    assets = _list_assets(["cloudfunctions.googleapis.com/Function"])
    lines  = [f"Cloud Functions ({len(assets)} total):", ""]

    for asset in assets:
        data           = _to_dict(asset.resource.data)
        build_config   = data.get("buildConfig")   or {}
        service_config = data.get("serviceConfig") or {}

        name        = data.get("name", asset.name).split("/")[-1]
        state       = data.get("state",       "UNKNOWN")
        runtime     = build_config.get("runtime",    "unknown")
        entry_point = build_config.get("entryPoint", "unknown")
        memory      = service_config.get("availableMemory",   "unknown")
        timeout     = service_config.get("timeoutSeconds",    "unknown")
        min_inst    = service_config.get("minInstanceCount",  0)
        max_inst    = service_config.get("maxInstanceCount",  "unlimited")
        sa          = service_config.get("serviceAccountEmail", "unknown")
        uri         = service_config.get("uri",               "unknown")
        env_vars    = service_config.get("environmentVariables") or {}
        updated     = data.get("updateTime", "unknown")

        lines.append(f"  Name:          {name}")
        lines.append(f"  State:         {state}")
        lines.append(f"  Runtime:       {runtime}")
        lines.append(f"  Entry Point:   {entry_point}")
        lines.append(f"  Memory:        {memory}")
        lines.append(f"  Timeout:       {timeout}s")
        lines.append(f"  Min Instances: {min_inst}")
        lines.append(f"  Max Instances: {max_inst}")
        lines.append(f"  Service SA:    {sa}")
        lines.append(f"  Trigger URL:   {uri}")
        if env_vars:
            lines.append("  Env Vars:")
            for k, v in env_vars.items():
                lines.append(f"    {k} = {v}")
        lines.append(f"  Last Updated:  {updated}")
        lines.append("")

    if not assets:
        lines.append("  (none found)")

    return "\n".join(lines)


def list_bucket_objects(args: dict) -> str:
    """List all objects in a Cloud Storage bucket.

    Raises:
        ToolInputError: If bucket_name is missing.
    """
    bucket_name = str(args.get("bucket_name", "")).strip()
    if not bucket_name:
        raise ToolInputError("bucket_name is required")

    client = _get_storage_client()
    blobs  = list(client.list_blobs(bucket_name))

    lines = [f"Objects in gs://{bucket_name} ({len(blobs)} total):", ""]
    for blob in blobs:
        size = blob.size or 0
        if size >= 1024 * 1024:
            size_str = f"{size / (1024 * 1024):.1f} MB"
        elif size >= 1024:
            size_str = f"{size / 1024:.1f} KB"
        else:
            size_str = f"{size} B"
        updated = (
            blob.updated.strftime("%Y-%m-%d %H:%M UTC")
            if blob.updated else "unknown"
        )
        lines.append(f"  {blob.name:<55}  {size_str:>10}  {updated}")

    if not blobs:
        lines.append("  (bucket is empty)")

    return "\n".join(lines)


# ==============================================================================
# Name → callable map used by mcp.py on tools/call.
# Adding a tool means: write the handler, add it to TOOL_REGISTRY, add it here.
# ==============================================================================

TOOL_FUNCTIONS = {
    "list_compute_instances":      list_compute_instances,
    "list_storage_buckets":        list_storage_buckets,
    "count_resources_by_type":     count_resources_by_type,
    "find_resources_by_label":     find_resources_by_label,
    "list_static_ip_addresses":    list_static_ip_addresses,
    "find_resources_by_type":      find_resources_by_type,
    "find_resources_by_region":    find_resources_by_region,
    "describe_resource":           describe_resource,
    "list_cloud_functions_detail": list_cloud_functions_detail,
    "list_bucket_objects":         list_bucket_objects,
}
