"""Tencent Cloud query/management tool using TENCENT_SECRET_ID / TENCENT_SECRET_KEY.

Exposes two tools wired into the capability registry:

- ``tencent_query(service, operation, parameters=None, region=None,
  confirm_write=False)`` \u2014 generic TencentCloud SDK call (CVM, regions/zones,
  etc.). Read-class operations (``Describe*``, ``Get*``, ``List*``,
  ``Inquiry*``, ``Search*``, ``Filter*``) run freely. **Write-class
  operations require ``confirm_write=true``** \u2014 mirroring the AWS tool's
  gate \u2014 and a hard denylist blocks catastrophic operations outright.

- ``cos_list_buckets()`` \u2014 lists COS buckets (qcloud_cos SDK). Read-only.

Credentials come from ``settings.tencent_secret_id`` /
``settings.tencent_secret_key`` (env ``TENCENT_SECRET_ID`` /
``TENCENT_SECRET_KEY``). When either is missing the tools return a clean
``{"status": "not_configured", ...}`` response \u2014 no crash, matching how
AWS/Gmail/SSH tools handle missing config.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import date, datetime
from typing import Any

from ..config import settings

logger = logging.getLogger("autopilot.tools.tencent_tools")

# Read-class verb allowlist \u2014 these run without confirm_write. TencentCloud
# mutating ops are prefixed Create*/Delete*/Update*/Modify*/Reset*/Run*/
# Terminate*/Start*/Stop*/Reboot*/Renew*/Associate*/Disassociate*/etc.
_READ_PREFIXES = (
    "Describe",
    "Get",
    "List",
    "Search",
    "Filter",
    "Lookup",
    "Head",
    "Query",
    "Inquiry",
    "BatchGet",
    "Scan",
)

# Catastrophic / hard-to-reverse operations: blocked outright, even with
# confirm_write (same spirit as aws_tools).
_DENYLISTED_SERVICES = {"account", "billing"}
_DENYLISTED_OPERATIONS = {
    "TerminateInstances",
    "DeleteInstances",
    "DeleteInstance",
    "DeleteKeyPairs",
    "DeleteBucket",
    "DeleteBuckets",
    "DeleteDisks",
    "ReleaseAddresses",
    "DeleteSecurityGroup",
    "DeleteVpc",
    "DeleteSubnet",
}


def _err(reason: str, **extra: Any) -> str:
    return json.dumps({"status": "error", "reason": reason, **extra})


def _camel_to_snake(name: str) -> str:
    """``DescribeZones`` \u2192 ``describe_zones`` (SDK client method name)."""
    import re

    s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


def _is_read_only(operation: str) -> bool:
    return any(operation.startswith(p) for p in _READ_PREFIXES)


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, bytes):
        return base64.b64encode(obj).decode("ascii")
    if hasattr(obj, "to_json_string"):
        try:
            return json.loads(obj.to_json_string())
        except Exception:
            return str(obj)
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    return str(obj)


def _client(service: str, region: str):
    """Lazily build a TencentCloud SDK client for ``service`` in ``region``."""
    from tencentcloud.common import credential
    from tencentcloud.common.profile.client_profile import ClientProfile
    from tencentcloud.common.profile.http_profile import HttpProfile

    cred = credential.Credential(
        settings.tencent_secret_id, settings.tencent_secret_key
    )
    http_profile = HttpProfile()
    http_profile.endpoint = _service_endpoint(service)
    client_profile = ClientProfile()
    client_profile.httpProfile = http_profile

    # service -> (module_path, client_class_name)
    _SERVICE_MODULES = {
        "cvm": ("tencentcloud.cvm.v20170312", "cvm_client", "CvmClient"),
        "region": ("tencentcloud.region.v20220627", "region_client", "RegionClient"),
        "vpc": ("tencentcloud.vpc.v20170312", "vpc_client", "VpcClient"),
        "cdb": ("tencentcloud.cdb.v20170320", "cdb_client", "CdbClient"),
        "clb": ("tencentcloud.clb.v20180317", "clb_client", "ClbClient"),
        "monitor": (
            "tencentcloud.monitor.v20180724",
            "monitor_client",
            "MonitorClient",
        ),
        "cos": None,  # handled by cos_list_buckets via qcloud_cos
    }
    spec = _SERVICE_MODULES.get(service)
    if spec is None:
        # Try generic import: tencentcloud.<service>.v20170312.<service>_client
        try:
            mod = __import__(
                f"tencentcloud.{service}.v20170312.{service}_client",
                fromlist=["*"],
            )
            return getattr(mod, service.title() + "Client")(
                cred, region, client_profile
            )
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"unsupported tencent service: {service} ({e})") from e

    mod_path, mod_name, cls_name = spec
    try:
        mod = __import__(f"{mod_path}.{mod_name}", fromlist=["*"])
    except ImportError as e:
        raise ValueError(f"tencent service module not found: {service} ({e})") from e
    return getattr(mod, cls_name)(cred, region, client_profile)


def _service_endpoint(service: str) -> str:
    """Map a service to its TencentCloud API endpoint domain."""
    _ENDPOINTS = {
        "cvm": "cvm.tencentcloudapi.com",
        "region": "region.tencentcloudapi.com",
        "vpc": "vpc.tencentcloudapi.com",
        "cdb": "cdb.tencentcloudapi.com",
        "clb": "clb.tencentcloudapi.com",
        "monitor": "monitor.tencentcloudapi.com",
    }
    return _ENDPOINTS.get(service, f"{service}.tencentcloudapi.com")


def _configured() -> bool:
    return bool(settings.tencent_secret_id and settings.tencent_secret_key)


def tencent_query(
    service: str,
    operation: str,
    parameters: dict | None = None,
    region: str | None = None,
    confirm_write: bool = False,
) -> str:
    """Call a TencentCloud SDK API (read-only by default; writes need confirm)."""
    if not service or not operation:
        return _err("service and operation are required")

    if service.lower() in _DENYLISTED_SERVICES or operation in _DENYLISTED_OPERATIONS:
        return _err(
            "operation is denylisted \u2014 catastrophic/account-level mutations are never allowed from this tool",
            service=service,
            operation=operation,
        )

    if not _is_read_only(operation) and not confirm_write:
        return _err(
            "write-class operation requires confirm_write=true \u2014 re-issue the "
            "call with confirm_write set if you are sure this mutation is "
            "intended (state WHAT will change and WHY in your reply)",
            operation=operation,
            read_prefixes=list(_READ_PREFIXES),
        )

    if not _configured():
        return json.dumps(
            {
                "status": "not_configured",
                "reason": "TENCENT_SECRET_ID / TENCENT_SECRET_KEY are not set in this instance's environment. "
                "Locations: /opt/truesight_autopilot/.env or /opt/bionpact_autopilot/.env (see "
                "agentic_ai_context/credentials/API_CREDENTIALS_DOCUMENTATION.md \u00a710.7).",
                "service": service,
                "operation": operation,
            }
        )

    try:
        client = _client(service, region or settings.tencent_region)
        method_name = _camel_to_snake(operation)
        method = getattr(client, method_name)
        req_class_name = operation + "Request"
        # Request classes live in tencentcloud.<service>.v20170312.models — derive
        # from the service name (deterministic), not the client instance, so tests
        # can stub the module cleanly.
        try:
            mod = __import__(f"tencentcloud.{service}.v20170312.models", fromlist=["*"])
            req_cls = getattr(mod, req_class_name)
        except (ImportError, AttributeError):
            req_module = type(client).__module__.rsplit(".", 1)[0]
            mod = __import__(req_module, fromlist=["*"])
            req_cls = getattr(mod, req_class_name)
        request = req_cls()
        if parameters:
            request.from_json_string(json.dumps(parameters))
        response = method(request)
        payload = json.loads(response.to_json_string())
        return json.dumps(
            {
                "status": "ok",
                "service": service,
                "operation": operation,
                "data": payload,
            }
        )
    except Exception as e:  # noqa: BLE001
        return _err(
            f"tencent {service}.{operation} failed: {e}",
            service=service,
            operation=operation,
        )


def cos_list_buckets() -> str:
    """List COS buckets via qcloud_cos (read-only)."""
    if not _configured():
        return json.dumps(
            {
                "status": "not_configured",
                "reason": "TENCENT_SECRET_ID / TENCENT_SECRET_KEY are not set in this instance's environment. "
                "Locations: /opt/truesight_autopilot/.env or /opt/bionpact_autopilot/.env (see "
                "agentic_ai_context/credentials/API_CREDENTIALS_DOCUMENTATION.md \u00a710.7).",
            }
        )
    try:
        from qcloud_cos import CosConfig, CosS3Client

        conf = CosConfig(
            Region=settings.tencent_region,
            SecretId=settings.tencent_secret_id,
            SecretKey=settings.tencent_secret_key,
        )
        client = CosS3Client(conf)
        result = client.list_buckets()
        buckets = result.get("Buckets", {}).get("Bucket", [])
        return json.dumps(
            {
                "status": "ok",
                "region": settings.tencent_region,
                "bucket_count": len(buckets),
                "buckets": [
                    {
                        "name": b.get("Name"),
                        "location": b.get("Location"),
                        "creation_date": b.get("CreationDate"),
                    }
                    for b in buckets
                ],
            }
        )
    except Exception as e:  # noqa: BLE001
        return _err(f"cos list_buckets failed: {e}")


def _dispatch(args: dict, ctx: dict) -> str:  # noqa: ARG001
    action = args.get("action", "")
    if action == "list_buckets":
        return cos_list_buckets()
    return tencent_query(
        service=args.get("service", ""),
        operation=args.get("operation", ""),
        parameters=args.get("parameters"),
        region=args.get("region"),
        confirm_write=bool(args.get("confirm_write", False)),
    )


from ..tool_registry import ToolSpec  # noqa: E402

TOOL_SPEC = ToolSpec(
    name="tencent_query",
    description=(
        "Tencent Cloud query/management. Read-only ops (Describe*/Get*/List*/Inquiry*/Search*) run freely; "
        "write-class ops (Create*/Run*/etc.) require confirm_write=true. Services: cvm, region, vpc, cdb, clb, "
        "monitor, or cos (cos only supports action='list_buckets'). Returns JSON. Degrades to "
        "'not_configured' when TENCENT_SECRET_ID/TENCENT_SECRET_KEY are unset."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "Optional sub-action. 'list_buckets' lists COS buckets (service/operation ignored).",
                "enum": ["list_buckets"],
            },
            "service": {
                "type": "string",
                "description": "TencentCloud service, e.g. 'cvm', 'region', 'vpc', 'cdb', 'clb', 'monitor'.",
            },
            "operation": {
                "type": "string",
                "description": "PascalCase TencentCloud API operation, e.g. 'DescribeZones', 'DescribeInstances', 'RunInstances'.",
            },
            "parameters": {
                "type": "object",
                "description": "Operation parameters as a JSON object (SDK request body).",
            },
            "region": {
                "type": "string",
                "description": "Override the default region (default TENCENT_REGION / ap-guangzhou).",
            },
            "confirm_write": {
                "type": "boolean",
                "description": "Required true for write-class (mutating / billable) operations. Leave unset for reads.",
                "default": False,
            },
        },
        "required": ["service", "operation"],
    },
    handler=lambda args, ctx: _dispatch(args, ctx),
)
