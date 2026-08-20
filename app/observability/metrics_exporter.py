"""Custom metrics exporter for the liquidation bot.

Runs as a sidecar container. Every ``METRICS_EXPORT_INTERVAL_SECONDS``
(default 60s) it scrapes the bot's ``/internal/observability`` endpoint and
``PutMetricData``s the values under the ``Twyne/liquidation-bot`` CloudWatch
namespace, one set of metrics per chain (dimension ``Chain``).

Mirrors twyne-data's ``metrics_exporter.py`` pattern: the bot is the single
source of truth for what each value means (it computes them from live
``AccountMonitor`` state); this process only maps JSON fields to CloudWatch
metrics and ships them. Thresholds/alerting live in Grafana.

Kept deliberately dumb and secret-free: it holds no signing key and only needs
the bot URL + AWS region. On EC2 it authenticates to CloudWatch via the
instance profile (the same role the CloudWatch agent already uses). NOTE: for
the container to reach IMDS for instance-role creds, the instance's metadata
hop limit must be >= 2 (or pass AWS_* creds explicitly in dev).

Metrics published (namespace ``Twyne/liquidation-bot``, dimension ``Chain``):

  MonitoredCVCount                 (Count)
  SecondsSinceLastCVCheck          (Seconds)   count-gated
  OldestCVCheckAgeSeconds          (Seconds)   count-gated
  CVsBelowExternalBoundaryCount    (Count)
  UnhealthyCVCount                 (Count)
  SignerBalanceEth                 (None)
  LatestScannedBlock               (Count)
  LiquidationFailureCount          (Count, monotonic — use delta() in Grafana)
  FailedInitCount                  (Count)
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import boto3
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("liqbot-metrics-exporter")

NAMESPACE = "Twyne/liquidation-bot"
INTERVAL_SECONDS = int(os.environ.get("METRICS_EXPORT_INTERVAL_SECONDS", "60"))
AWS_REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "eu-north-1"
BOT_URL = os.environ.get("METRICS_BOT_URL", "http://liquidation-bot:8080/internal/observability")
SCRAPE_TIMEOUT_SECONDS = float(os.environ.get("METRICS_SCRAPE_TIMEOUT_SECONDS", "10"))
DRY_RUN = os.environ.get("METRICS_DRY_RUN", "").lower() in ("1", "true", "yes")

# Chain id (as string, the JSON key) -> CloudWatch dimension label.
CHAIN_LABELS = {"1": "mainnet"}

# (json_field, MetricName, Unit). Every field the endpoint may return is mapped;
# count-gated fields (seconds_since_last_cv_check, oldest_cv_check_age_seconds)
# and signer_balance_eth are simply absent from the snapshot when unavailable,
# so they are skipped automatically — yielding NoData (not a false alert).
METRIC_MAP = [
    ("monitored_cv_count", "MonitoredCVCount", "Count"),
    ("seconds_since_last_cv_check", "SecondsSinceLastCVCheck", "Seconds"),
    ("oldest_cv_check_age_seconds", "OldestCVCheckAgeSeconds", "Seconds"),
    ("cvs_below_external_boundary", "CVsBelowExternalBoundaryCount", "Count"),
    ("unhealthy_cv_count", "UnhealthyCVCount", "Count"),
    ("signer_balance_eth", "SignerBalanceEth", "None"),
    ("latest_scanned_block", "LatestScannedBlock", "Count"),
    ("liquidation_failure_count", "LiquidationFailureCount", "Count"),
    ("failed_init_count", "FailedInitCount", "Count"),
]


def build_metric_data(payload: dict) -> list[dict[str, Any]]:
    """Map a /internal/observability payload to CloudWatch MetricData entries."""
    metric_data: list[dict[str, Any]] = []
    for chain_id, snap in (payload.get("chains") or {}).items():
        if not isinstance(snap, dict) or "error" in snap:
            logger.warning("skipping chain %s: %s", chain_id, snap.get("error") if isinstance(snap, dict) else snap)
            continue
        label = CHAIN_LABELS.get(str(chain_id), str(chain_id))
        for field, metric_name, unit in METRIC_MAP:
            if field not in snap or snap[field] is None:
                continue
            metric_data.append(
                {
                    "MetricName": metric_name,
                    "Value": float(snap[field]),
                    "Unit": unit,
                    "Dimensions": [{"Name": "Chain", "Value": label}],
                }
            )
    return metric_data


def emit(cw: Any, metric_data: list[dict[str, Any]]) -> None:
    """PutMetricData with batching — CloudWatch caps at 1000 entries/request."""
    BATCH = 20
    for i in range(0, len(metric_data), BATCH):
        chunk = metric_data[i : i + BATCH]
        if DRY_RUN:
            logger.info(
                "[dry-run] would PutMetricData %s entries: %s",
                len(chunk),
                [(m["MetricName"], m["Value"], m["Dimensions"][0]["Value"]) for m in chunk],
            )
        else:
            cw.put_metric_data(Namespace=NAMESPACE, MetricData=chunk)


def scrape_once(cw: Any) -> int:
    """One scrape→emit cycle. Returns number of metrics emitted (0 on failure)."""
    try:
        resp = requests.get(BOT_URL, timeout=SCRAPE_TIMEOUT_SECONDS)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as ex:
        logger.error("scrape failed (%s): %s", BOT_URL, ex)
        return 0
    metric_data = build_metric_data(payload)
    if not metric_data:
        logger.info("no metrics to emit this cycle")
        return 0
    try:
        emit(cw, metric_data)
    except Exception as ex:
        logger.error("put_metric_data failed: %s", ex)
        return 0
    logger.info("emitted %s metrics", len(metric_data))
    return len(metric_data)


def main() -> None:
    logger.info(
        "liqbot metrics exporter starting: url=%s namespace=%s interval=%ss region=%s dry_run=%s",
        BOT_URL,
        NAMESPACE,
        INTERVAL_SECONDS,
        AWS_REGION,
        DRY_RUN,
    )
    cw = None if DRY_RUN else boto3.client("cloudwatch", region_name=AWS_REGION)
    while True:
        scrape_once(cw)
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
