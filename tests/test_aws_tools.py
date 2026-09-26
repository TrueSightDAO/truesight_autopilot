"""Unit tests for aws_query — exercises the read-only allowlist + dispatch."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from app.tools import aws_tools


def _stub_specs(label="explorya"):
    return [
        {
            "label": label,
            "key_id": "AKIA-TEST",
            "secret": "secret-test",
            "region": "us-east-1",
        }
    ]


def test_write_class_operation_requires_confirm_write(monkeypatch):
    out = json.loads(
        aws_tools.aws_query(
            account="explorya",
            service="ec2",
            operation="TerminateInstances",
        )
    )
    assert out["status"] == "error"
    assert "confirm_write" in out["reason"]


def test_write_class_operation_dispatches_with_confirm_write(monkeypatch):
    monkeypatch.setattr("app.aws_monitor.read_account_specs", lambda: _stub_specs())
    fake_client = MagicMock()
    fake_client.reboot_instances.return_value = {
        "ResponseMetadata": {"RequestId": "req-w1"},
    }
    fake_session = MagicMock()
    fake_session.client.return_value = fake_client

    with patch("boto3.Session", return_value=fake_session):
        out = json.loads(
            aws_tools.aws_query(
                account="explorya",
                service="ec2",
                operation="RebootInstances",
                parameters={"InstanceIds": ["i-1"]},
                confirm_write=True,
            )
        )

    assert out["status"] == "ok"
    fake_client.reboot_instances.assert_called_once_with(InstanceIds=["i-1"])


def test_denylisted_operation_blocked_even_with_confirm_write():
    out = json.loads(
        aws_tools.aws_query(
            account="explorya",
            service="route53",
            operation="DeleteHostedZone",
            confirm_write=True,
        )
    )
    assert out["status"] == "error"
    assert "denylisted" in out["reason"]


def test_denylisted_service_blocked():
    out = json.loads(
        aws_tools.aws_query(
            account="explorya",
            service="organizations",
            operation="ListAccounts",  # even reads — org service has no SRE use
        )
    )
    assert out["status"] == "error"
    assert "denylisted" in out["reason"]


def test_unknown_account_returns_error(monkeypatch):
    monkeypatch.setattr("app.aws_monitor.read_account_specs", lambda: _stub_specs())
    out = json.loads(
        aws_tools.aws_query(
            account="ghost-account",
            service="ec2",
            operation="DescribeInstances",
        )
    )
    assert out["status"] == "error"
    assert "unknown AWS account" in out["reason"]
    assert "explorya" in out["available"]


def test_read_only_operation_dispatches(monkeypatch):
    monkeypatch.setattr("app.aws_monitor.read_account_specs", lambda: _stub_specs())

    fake_client = MagicMock()
    fake_client.describe_instances.return_value = {
        "Reservations": [{"Instances": [{"InstanceId": "i-1"}]}],
        "ResponseMetadata": {"RequestId": "req-123"},
    }
    fake_session = MagicMock()
    fake_session.client.return_value = fake_client

    with patch("boto3.Session", return_value=fake_session):
        out = json.loads(
            aws_tools.aws_query(
                account="explorya",
                service="ec2",
                operation="DescribeInstances",
            )
        )

    assert out["status"] == "ok"
    assert out["account"] == "explorya"
    assert out["operation"] == "DescribeInstances"
    assert out["request_id"] == "req-123"
    assert out["response"]["Reservations"][0]["Instances"][0]["InstanceId"] == "i-1"
    fake_client.describe_instances.assert_called_once_with()


def test_parameters_passed_through(monkeypatch):
    monkeypatch.setattr("app.aws_monitor.read_account_specs", lambda: _stub_specs())
    fake_client = MagicMock()
    fake_client.list_buckets.return_value = {"Buckets": [{"Name": "b1"}]}
    fake_session = MagicMock()
    fake_session.client.return_value = fake_client

    with patch("boto3.Session", return_value=fake_session):
        out = json.loads(
            aws_tools.aws_query(
                account="explorya",
                service="s3",
                operation="ListBuckets",
                parameters={"MaxBuckets": 5},
            )
        )

    assert out["status"] == "ok"
    fake_client.list_buckets.assert_called_once_with(MaxBuckets=5)


def test_camel_to_snake():
    assert aws_tools._camel_to_snake("DescribeInstances") == "describe_instances"
    assert aws_tools._camel_to_snake("GetCostAndUsage") == "get_cost_and_usage"
    assert aws_tools._camel_to_snake("ListS3Buckets") == "list_s3_buckets"
    assert aws_tools._camel_to_snake("HeadObject") == "head_object"
