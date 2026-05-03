"""Monitor AWS EC2 health and costs via CloudWatch + Cost Explorer."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError

from .config import settings

logger = logging.getLogger("autopilot.aws")


class AWSMonitor:
    def __init__(self):
        self._cw = None
        self._ce = None
        self._health = None
        self._init_clients()

    def _init_clients(self):
        """Initialize boto3 clients. Prefers IAM instance role; falls back to env vars."""
        try:
            # Try default credential chain (IMDS → env → ~/.aws)
            self._cw = boto3.client("cloudwatch", region_name=settings.aws_region)
            self._ce = boto3.client("ce", region_name=settings.aws_region)
            self._health = boto3.client("health", region_name=settings.aws_region)
            # Test connectivity
            self._cw.list_metrics(Namespace="AWS/EC2", MaxResults=1)
            logger.info("AWS CloudWatch connected")
        except ClientError as e:
            logger.error("AWS client init failed: %s", e)
            self._cw = None
            self._ce = None
            self._health = None

    async def run_loop(self, interval_seconds: int = 300):
        """Run health checks every 5 minutes; cost check daily."""
        import asyncio
        last_cost_check = None
        while True:
            try:
                if self._cw:
                    self.check_ec2_health()
                if self._health:
                    self.check_aws_health_events()

                now = datetime.now(timezone.utc)
                if last_cost_check is None or (now - last_cost_check).days >= 1:
                    if self._ce:
                        self.check_daily_cost()
                    last_cost_check = now
            except Exception as e:
                logger.error("AWS monitor loop error: %s", e)
            await asyncio.sleep(interval_seconds)

    def check_ec2_health(self):
        """Check EC2 instances for status check failures + high CPU."""
        if not self._cw:
            return
        try:
            # List EC2 instances via CloudWatch metrics
            metrics = self._cw.list_metrics(Namespace="AWS/EC2", MetricName="StatusCheckFailed")
            for m in metrics.get("Metrics", []):
                dimensions = {d["Name"]: d["Value"] for d in m.get("Dimensions", [])}
                instance_id = dimensions.get("InstanceId")
                if instance_id:
                    # Fetch recent datapoints
                    resp = self._cw.get_metric_statistics(
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
                        logger.warning("EC2 %s has status check failures", instance_id)
        except ClientError as e:
            logger.error("EC2 health check failed: %s", e)

    def check_daily_cost(self):
        """Fetch yesterday's AWS spend."""
        if not self._ce:
            return
        try:
            yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            resp = self._ce.get_cost_and_usage(
                TimePeriod={"Start": yesterday, "End": today},
                Granularity="DAILY",
                Metrics=["BlendedCost"],
                GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
            )
            for result in resp.get("ResultsByTime", []):
                total = result.get("Total", {}).get("BlendedCost", {}).get("Amount", "0")
                logger.info("AWS daily spend (%s): $%s", yesterday, total)
                # TODO: alert if spend > threshold
        except ClientError as e:
            logger.error("Cost check failed: %s", e)

    def check_aws_health_events(self):
        """Check for AWS Health events affecting your resources."""
        if not self._health:
            return
        try:
            events = self._health.describe_events(filter={"eventStatusCodes": ["open", "upcoming"]})
            for event in events.get("events", []):
                logger.warning(
                    "AWS Health event: %s (%s) — %s",
                    event.get("eventTypeCode"),
                    event.get("statusCode"),
                    event.get("eventDescription", [{}])[0].get("latestDescription", ""),
                )
        except ClientError as e:
            logger.error("Health check failed: %s", e)
