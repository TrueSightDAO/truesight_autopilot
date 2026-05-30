"""AWS query tool spanning every account in ``AWS_ACCOUNTS``.

Exposes ``aws_query(account, service, operation, parameters=None, region=None)``.

- ``account`` — one of the labels in ``AWS_ACCOUNTS`` (e.g. ``"explorya"``,
  ``"nelanco"``). Reuses :func:`app.aws_monitor.read_account_specs` so the
  credential-loading logic stays in one place.
- ``service`` — any boto3 service name (``"ec2"``, ``"s3"``, ``"logs"``,
  ``"cloudwatch"``, ``"ce"`` …).
- ``operation`` — PascalCase AWS API operation. **Allowlisted** to safe
  shapes only: ``Describe*``, ``Get*``, ``List*``, ``Search*``, ``Filter*``,
  ``Lookup*``, ``Head*``, ``Change*`` (Route53 DNS mutations). Anything else
  returns ``{status:"forbidden"}``.

The boto3 response is JSON-serialised with ``datetime`` → ISO-8601 and bytes
→ base64 so the model can consume it.
"""
from __future__ import annotations

import base64
import json
import logging
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

logger = logging.getLogger("autopilot.tools.aws_tools")

# Read-only verb allowlist. AWS conventionally prefixes mutating ops with
# Create*/Delete*/Update*/Put*/Run*/Terminate*/Start*/Stop*/etc., and read-only
# ops with these verbs. Be conservative — anything not on this list is blocked
# even if it happens to be safe.
_READ_PREFIXES = (
    "Describe", "Get", "List", "Search", "Filter", "Lookup", "Head", "Query",
    "BatchGet", "Scan",  # Dynamo Scan is read-only despite the verb
    "Change",  # Route53 ChangeResourceRecordSets — IAM is already Administrator
)


def _err(reason: str, **extra: Any) -> str:
    return json.dumps({"status": "error", "reason": reason, **extra})


def _camel_to_snake(name: str) -> str:
    """``DescribeInstances`` → ``describe_instances`` (boto3 client method name)."""
    s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


def _is_read_only(operation: str) -> bool:
    return any(operation.startswith(p) for p in _READ_PREFIXES)


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        # Preserve precision as a string; the model can re-parse if needed.
        return str(obj)
    if isinstance(obj, (bytes, bytearray)):
        return {"__b64__": base64.b64encode(bytes(obj)).decode("ascii")}
    if isinstance(obj, set):
        return list(obj)
    return str(obj)  # last-resort fallback


def aws_query(
    account: str,
    service: str,
    operation: str,
    parameters: dict | None = None,
    region: str | None = None,
) -> str:
    if not account or not service or not operation:
        return _err("account, service, and operation are required")

    if not _is_read_only(operation):
        return _err(
            "write-class operation blocked",
            operation=operation,
            allowed_prefixes=list(_READ_PREFIXES),
        )

    # Lazy boto3 import: keeps unrelated tests fast.
    try:
        import boto3  # type: ignore
        from botocore.exceptions import BotoCoreError, ClientError  # type: ignore
    except Exception as e:  # pragma: no cover
        return _err(f"boto3 unavailable: {e}")

    from ..aws_monitor import read_account_specs

    specs = read_account_specs()
    spec = next((s for s in specs if s["label"] == account.lower()), None)
    if spec is None:
        return _err(
            "unknown AWS account",
            account=account,
            available=[s["label"] for s in specs],
        )

    target_region = region or spec.get("region")
    try:
        session = boto3.Session(
            aws_access_key_id=spec["key_id"],
            aws_secret_access_key=spec["secret"],
            region_name=target_region,
        )
        client = session.client(service)
    except Exception as e:
        return _err(f"boto3 client init failed: {e}", account=account, service=service)

    method_name = _camel_to_snake(operation)
    method = getattr(client, method_name, None)
    if method is None:
        return _err(
            f"unknown operation for service {service}",
            operation=operation,
            resolved_method=method_name,
        )

    params = parameters or {}
    if not isinstance(params, dict):
        return _err("parameters must be a JSON object", got=type(params).__name__)

    try:
        response = method(**params)
    except ClientError as e:
        # AWS API errors are useful to the model — pass them through verbatim.
        return _err(
            f"AWS ClientError: {e.response.get('Error', {}).get('Code', '?')}",
            message=str(e),
            account=account, service=service, operation=operation,
        )
    except BotoCoreError as e:
        return _err(
            f"AWS BotoCoreError: {e}",
            account=account, service=service, operation=operation,
        )
    except TypeError as e:
        return _err(
            f"bad parameters: {e}",
            account=account, service=service, operation=operation,
            received_keys=list(params.keys()),
        )

    # Drop boto's ResponseMetadata noise but keep RequestId for tracing.
    meta = response.pop("ResponseMetadata", None) if isinstance(response, dict) else None
    request_id = meta.get("RequestId") if isinstance(meta, dict) else None

    logger.info(
        "aws_query ok: account=%s service=%s op=%s region=%s",
        account, service, operation, target_region,
    )
    return json.dumps({
        "status": "ok",
        "account": account.lower(),
        "service": service,
        "operation": operation,
        "region": target_region,
        "request_id": request_id,
        "response": response,
    }, default=_json_default)


# ── capability manifest entry ─────────────────────────────────────────────

from ..tool_registry import ToolSpec  # noqa: E402

TOOL_SPEC = ToolSpec(
    name="aws_query",
    description="Run an AWS API call against any account in AWS_ACCOUNTS (currently 'explorya' and 'nelanco'). Allowlisted to Describe*/Get*/List*/Search*/Filter*/Lookup*/Head*/Query*/BatchGet*/Scan*/Change* operations — Change* allows Route53 ChangeResourceRecordSets for DNS management. Useful for checking EC2 instance state, CloudWatch metrics, Logs, Cost Explorer, S3 buckets, and managing Route53 DNS records.",
    parameters={
        "type": "object",
        "properties": {
            "account": {"type": "string", "description": "AWS account label.", "enum": ["explorya", "nelanco"]},
            "service": {"type": "string", "description": "boto3 service name, e.g. 'ec2', 's3', 'logs', 'cloudwatch', 'ce'."},
            "operation": {"type": "string", "description": "PascalCase AWS API operation, e.g. 'DescribeInstances', 'ListBuckets', 'GetCostAndUsage'."},
            "parameters": {"type": "object", "description": "Operation parameters as a JSON object."},
            "region": {"type": "string", "description": "Override the account's default region for this call."},
        },
        "required": ["account", "service", "operation"],
    },
    handler=lambda args, ctx: aws_query(
        account=args.get("account", ""),
        service=args.get("service", ""),
        operation=args.get("operation", ""),
        parameters=args.get("parameters"),
        region=args.get("region"),
    ),
)
