"""Unit tests for tencent_query / cos_list_buckets \u2014 gate + degradation."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from app.tools import tencent_tools
from app.tools.tencent_tools import settings


def _configure():
    settings.tencent_secret_id = "AKID-test"
    settings.tencent_secret_key = "secret-test"
    settings.tencent_region = "ap-guangzhou"


def _unconfigure():
    settings.tencent_secret_id = None
    settings.tencent_secret_key = None


def test_write_class_operation_requires_confirm_write():
    _configure()
    out = json.loads(
        tencent_tools.tencent_query(service="cvm", operation="RunInstances")
    )
    assert out["status"] == "error"
    assert "confirm_write" in out["reason"]


def test_write_class_operation_dispatches_with_confirm_write():
    _configure()
    import types

    fake_client = MagicMock()
    fake_client.run_instances.return_value.to_json_string.return_value = (
        '{"InstanceIdSet": ["ins-1"], "RequestId": "req-1"}'
    )
    fake_req_mod = types.ModuleType("tencentcloud.cvm.v20170312.models")
    fake_req_cls = type(
        "RunInstancesRequest",
        (),
        {"from_json_string": lambda self, payload: None},
    )
    fake_req_mod.RunInstancesRequest = fake_req_cls

    with (
        patch.object(tencent_tools, "_client", return_value=fake_client),
        patch.dict(
            __import__("sys").modules,
            {"tencentcloud.cvm.v20170312.models": fake_req_mod},
        ),
    ):
        out = json.loads(
            tencent_tools.tencent_query(
                service="cvm",
                operation="RunInstances",
                parameters={"InstanceType": "S5.SMALL1", "ImageId": "img-123"},
                confirm_write=True,
            )
        )
    assert out["status"] == "ok"
    fake_client.run_instances.assert_called_once()


def test_denylisted_operation_blocked_even_with_confirm_write():
    _configure()
    out = json.loads(
        tencent_tools.tencent_query(
            service="cvm", operation="TerminateInstances", confirm_write=True
        )
    )
    assert out["status"] == "error"
    assert "denylisted" in out["reason"]


def test_not_configured_degrades_cleanly_for_read():
    _unconfigure()
    out = json.loads(
        tencent_tools.tencent_query(service="cvm", operation="DescribeZones")
    )
    assert out["status"] == "not_configured"
    assert "TENCENT_SECRET_ID" in out["reason"]


def test_not_configured_degrades_cleanly_for_cos():
    _unconfigure()
    out = json.loads(tencent_tools.cos_list_buckets())
    assert out["status"] == "not_configured"


def test_read_requires_service_and_operation():
    _configure()
    out = json.loads(tencent_tools.tencent_query(service="", operation=""))
    assert out["status"] == "error"
    assert "service and operation are required" in out["reason"]


def test_cos_list_buckets_ok():
    _configure()
    fake_client = MagicMock()
    fake_client.list_buckets.return_value = {
        "Buckets": {
            "Bucket": [
                {
                    "Name": "test-bucket-1250000000",
                    "Location": "ap-guangzhou",
                    "CreationDate": "2026-01-01T00:00:00Z",
                }
            ]
        }
    }
    with patch("qcloud_cos.CosS3Client", return_value=fake_client) as mock_cls:
        out = json.loads(tencent_tools.cos_list_buckets())
    assert out["status"] == "ok"
    assert out["bucket_count"] == 1
    assert out["buckets"][0]["name"] == "test-bucket-1250000000"
    mock_cls.assert_called_once()


def test_dispatch_list_buckets_action():
    _configure()
    fake_client = MagicMock()
    fake_client.list_buckets.return_value = {"Buckets": {"Bucket": []}}
    with patch("qcloud_cos.CosS3Client", return_value=fake_client):
        out = json.loads(tencent_tools._dispatch({"action": "list_buckets"}, {}))
    assert out["status"] == "ok"


def test_pascalcase_client_method_fallback():
    """Real SDK (>=3.1.x) exposes PascalCase methods (DescribeZones); snake_case
    aliases were removed. Dispatch must fall back to the operation name."""
    import types

    _configure()
    fake_client = MagicMock()
    # NOTE: no describe_zones attribute — only the PascalCase method exists,
    # mirroring tencentcloud-sdk-python 3.1.166 where snake aliases are gone.
    del fake_client.describe_zones
    fake_client.DescribeZones.return_value.to_json_string.return_value = (
        '{"ZoneSet": [], "RequestId": "req-1"}'
    )
    fake_req_mod = types.ModuleType("tencentcloud.cvm.v20170312.models")
    fake_req_cls = type(
        "DescribeZonesRequest",
        (),
        {"from_json_string": lambda self, payload: None},
    )
    fake_req_mod.DescribeZonesRequest = fake_req_cls

    with (
        patch.object(tencent_tools, "_client", return_value=fake_client),
        patch.dict(
            __import__("sys").modules,
            {"tencentcloud.cvm.v20170312.models": fake_req_mod},
        ),
    ):
        out = json.loads(
            tencent_tools.tencent_query(service="cvm", operation="DescribeZones")
        )
    assert out["status"] == "ok", out
    fake_client.DescribeZones.assert_called_once()
