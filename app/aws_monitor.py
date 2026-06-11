"""Monitor AWS EC2 health and costs via CloudWatch + Cost Explorer.

Multi-account support
---------------------
TrueSight DAO operates AWS workloads across multiple accounts contributed by
different DAO members. Account labels are the contributing member's name
(lowercased) so log lines clearly identify *whose* account a signal came from:

- ``nelanco``   — Nelanco-provided account (formerly aliased ``CYPHER_DEFENCE``)
- ``explorya``  — Explorya-provided account (formerly aliased
  ``TRUESIGHT_DAO_AUTOPILOT``; account ``440626669078`` per
  ``agentic_ai_context/API_CREDENTIALS_DOCUMENTATION.md``)

Set ``AWS_ACCOUNTS=nelanco,explorya`` (or any subset) to enable monitoring.
For each ``LABEL`` (uppercased in the env-var key, lowercased in log
prefixes), provide:

- ``AWS_ACCESS_KEY_ID_<LABEL>``      (uppercase env-var)
- ``AWS_SECRET_ACCESS_KEY_<LABEL>``  (uppercase env-var)
- ``AWS_REGION_<LABEL>``             (optional; defaults to global ``AWS_REGION``)

Backwards-compat: if ``AWS_ACCOUNTS`` is unset, falls back to the legacy
single-account env (``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` /
``AWS_REGION``) under the implicit label ``default``. Existing single-account
deployments behave identically.

All log lines from this module are prefixed with ``[<label>]`` so journal
output stays disambiguated when monitoring multiple accounts.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

from .config import settings

logger = logging.getLogger("autopilot.aws")


def read_account_specs() -> list[dict]:
    """Returns a list of {label, key_id, secret, region} dicts.

    Empty list when no credentials are configured anywhere — caller logs once
    and skips the polling loop entirely (matches the existing behavior when
    ``AWS_*`` env vars were missing).
    """
    raw_labels = os.environ.get("AWS_ACCOUNTS", "").strip()
    specs: list[dict] = []
    if raw_labels:
        for raw in raw_labels.split(","):
            label = raw.strip()
            if not label:
                continue
            upper = label.upper()
            key_id = os.environ.get(f"AWS_ACCESS_KEY_ID_{upper}", "").strip()
            secret = os.environ.get(f"AWS_SECRET_ACCESS_KEY_{upper}", "").strip()
            region = (
                os.environ.get(f"AWS_REGION_{upper}", "").strip() or settings.aws_region
            )
            if not key_id or not secret:
                logger.warning(
                    "[%s] AWS_ACCESS_KEY_ID_%s or AWS_SECRET_ACCESS_KEY_%s missing — skipping",
                    label.lower(),
                    upper,
                    upper,
                )
                continue
            specs.append(
                {
                    "label": label.lower(),
                    "key_id": key_id,
                    "secret": secret,
                    "region": region,
                }
            )
        return specs

    # Backwards-compat: legacy single-account env, label='default'.
    if settings.aws_access_key_id and settings.aws_secret_access_key:
        specs.append(
            {
                "label": "default",
                "key_id": settings.aws_access_key_id,
                "secret": settings.aws_secret_access_key,
                "region": settings.aws_region,
            }
        )
    return specs


# Deprecated alias retained for any external import; new code should use the
# public name. Will be removed once the autopilot codebase is fully migrated.
_read_account_specs = read_account_specs


class _AccountClients:
    """Per-account boto3 clients + per-account state (e.g. health-API gating)."""

    def __init__(self, label: str, key_id: str, secret: str, region: str):
        self.label = label
        self.region = region
        self.account_id: str | None = None
        self.cw = None
        self.ce = None
        self.health = None
        # AWS Health API requires Business/Enterprise support; per-account flag.
        self.health_unsupported = False
        try:
            session = boto3.Session(
                aws_access_key_id=key_id,
                aws_secret_access_key=secret,
                region_name=region,
            )
            self.cw = session.client("cloudwatch")
            self.ce = session.client("ce")
            self.health = session.client("health")
            sts = session.client("sts")
            ident = sts.get_caller_identity()
            self.account_id = ident.get("Account")
            # Test connectivity (CloudWatch is the gating call — fast + cheap)
            self.cw.list_metrics(Namespace="AWS/EC2")
            logger.info(
                "[%s] AWS CloudWatch connected (account %s, region %s)",
                self.label,
                self.account_id,
                region,
            )
        except NoCredentialsError:
            logger.warning("[%s] AWS init failed: no credentials located", self.label)
            self._reset()
        except ClientError as e:
            logger.error("[%s] AWS client init failed: %s", self.label, e)
            self._reset()

    def _reset(self):
        self.cw = None
        self.ce = None
        self.health = None


class AWSMonitor:
    def __init__(self):
        self._accounts: list[_AccountClients] = []
        self._init_clients()

    def _init_clients(self):
        specs = read_account_specs()
        if not specs:
            logger.warning(
                "AWS monitor: no accounts configured (set AWS_ACCOUNTS=label1,label2,... "
                "with AWS_ACCESS_KEY_ID_<LABEL>/AWS_SECRET_ACCESS_KEY_<LABEL>, "
                "or the legacy AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY env vars)"
            )
            return
        for spec in specs:
            self._accounts.append(_AccountClients(**spec))

    async def run_loop(self, interval_seconds: int = 300):
        """Run health checks every 5 minutes; cost check daily.

        Iterates over every configured account each tick. Account-level errors
        don't stop the loop — they're logged and the next account's checks
        proceed.
        """
        import asyncio

        last_cost_check = None
        while True:
            try:
                for acct in self._accounts:
                    if acct.cw:
                        self.check_ec2_health(acct)
                    if acct.health:
                        self.check_aws_health_events(acct)

                now = datetime.now(timezone.utc)
                if last_cost_check is None or (now - last_cost_check).days >= 1:
                    for acct in self._accounts:
                        if acct.ce:
                            self.check_daily_cost(acct)
                    last_cost_check = now
            except Exception as e:
                logger.error("AWS monitor loop error: %s", e)
            await asyncio.sleep(interval_seconds)

    def check_ec2_health(self, acct: _AccountClients):
        """Check EC2 instances for status check failures + high CPU."""
        if not acct.cw:
            return
        try:
            metrics = acct.cw.list_metrics(
                Namespace="AWS/EC2", MetricName="StatusCheckFailed"
            )
            for m in metrics.get("Metrics", []):
                dimensions = {d["Name"]: d["Value"] for d in m.get("Dimensions", [])}
                instance_id = dimensions.get("InstanceId")
                if instance_id:
                    resp = acct.cw.get_metric_statistics(
                        Namespace="AWS/EC2",
                        MetricName="StatusCheckFailed",
                        Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
                        StartTime=datetime.now(timezone.utc) - timedelta(hours=1),
                        EndTime=datetime.now(timezone.utc),
                        Period=3600,
                        Statistics=["Average"],
                    )
                    datapoints = resp.get("Datapoints", [])
                    if any(d["Average"] > 0 for d in datapoints):
                        logger.warning(
                            "[%s] EC2 %s has status check failures",
                            acct.label,
                            instance_id,
                        )
        except ClientError as e:
            logger.error("[%s] EC2 health check failed: %s", acct.label, e)

    def check_daily_cost(self, acct: _AccountClients):
        """Fetch yesterday's AWS spend for this account."""
        if not acct.ce:
            return
        try:
            yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime(
                "%Y-%m-%d"
            )
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            resp = acct.ce.get_cost_and_usage(
                TimePeriod={"Start": yesterday, "End": today},
                Granularity="DAILY",
                Metrics=["BlendedCost"],
                GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
            )
            for result in resp.get("ResultsByTime", []):
                total = (
                    result.get("Total", {}).get("BlendedCost", {}).get("Amount", "0")
                )
                logger.info(
                    "[%s] AWS daily spend (%s): $%s", acct.label, yesterday, total
                )
                # TODO: alert if spend > threshold
        except ClientError as e:
            logger.error("[%s] Cost check failed: %s", acct.label, e)

    def check_aws_health_events(self, acct: _AccountClients):
        """Check for AWS Health events affecting your resources.

        Gracefully degrades on accounts without Business/Enterprise support
        (which is the AWS Health API prerequisite). Logs the limitation
        once per account, then becomes a no-op for the rest of the process
        lifetime for that specific account.
        """
        if not acct.health or acct.health_unsupported:
            return
        try:
            events = acct.health.describe_events(
                filter={"eventStatusCodes": ["open", "upcoming"]}
            )
            for event in events.get("events", []):
                logger.warning(
                    "[%s] AWS Health event: %s (%s) — %s",
                    acct.label,
                    event.get("eventTypeCode"),
                    event.get("statusCode"),
                    event.get("eventDescription", [{}])[0].get("latestDescription", ""),
                )
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "SubscriptionRequiredException":
                # Account doesn't have Business/Enterprise support — log once
                # at INFO and disable subsequent calls for this account.
                acct.health_unsupported = True
                logger.info(
                    "[%s] AWS Health API unavailable (account lacks Business support). "
                    "CloudWatch + Cost Explorer monitoring continues; Health polling disabled.",
                    acct.label,
                )
            else:
                logger.error("[%s] Health check failed: %s", acct.label, e)
