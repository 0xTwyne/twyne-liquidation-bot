"""
Creates and returns main flask app
"""

import os
import signal
import threading
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify
from flask_cors import CORS

from .liquidation.logging_config import setup_logger
from .liquidation.routes import liquidation, start_monitor

logger = setup_logger()


def _register_shutdown_handler():
    """Register SIGTERM/SIGINT handlers that gracefully stop the ChainManager
    (stop listeners → drain workers → final save_state). Signal handlers can only
    be installed from the main thread, so this is a no-op when create_app() is
    imported off the main thread (e.g. under some gunicorn worker models); in that
    case the OS default signal disposition applies and the periodic save remains
    the persistence backstop.

    The handler looks the ChainManager up lazily because it is created
    asynchronously inside the monitor thread after create_app returns."""
    if threading.current_thread() is not threading.main_thread():
        logger.info("Not on main thread; skipping SIGTERM/SIGINT handler registration.")
        return

    def _handle_shutdown(signum, _frame):
        logger.info("Received signal %s; shutting down ChainManager gracefully.", signum)
        chain_manager = getattr(start_monitor, "_chain_manager", None)
        if chain_manager is not None:
            try:
                chain_manager.stop()
            except Exception as ex:  # noqa: BLE001 - best-effort shutdown
                logger.error("Error during graceful shutdown: %s", ex, exc_info=True)
        # Re-raise default behaviour so the process actually exits.
        raise SystemExit(0)

    try:
        signal.signal(signal.SIGTERM, _handle_shutdown)
        signal.signal(signal.SIGINT, _handle_shutdown)
        logger.info("Registered SIGTERM/SIGINT graceful-shutdown handlers.")
    except ValueError as ex:
        # signal.signal raises ValueError if not on the main thread.
        logger.warning("Could not register signal handlers: %s", ex)


# How long the bot's main work loop can be silent before /health flips to 503.
# Both AccountMonitor._process_account_update and the FactoryListener's
# polling loop refresh _last_activity_at, so this must be comfortably longer
# than 2 × FactoryListener.config.SCAN_INTERVAL (300 s in config.yaml).
# Default: 2 × 300 s + 120 s margin = 720 s. Override with HEALTH_STALE_SECONDS.
HEALTH_STALE_THRESHOLD = timedelta(seconds=int(os.environ.get("HEALTH_STALE_SECONDS", "720")))


def _parse_chain_ids() -> list:
    """Read monitored chain IDs from MONITORED_CHAIN_IDS env var (comma-separated).
    Defaults to chain 1 (Ethereum mainnet) when the variable is not set.

    Example: MONITORED_CHAIN_IDS=1. Comma-separate IDs to monitor more chains
    once they have a block under `chains:` in app/config.yaml.
    """
    raw = os.environ.get("MONITORED_CHAIN_IDS", "1")
    ids = [int(cid.strip()) for cid in raw.split(",") if cid.strip()]
    if not ids:
        logger.warning("MONITORED_CHAIN_IDS is set but empty; defaulting to chain 1.")
        return [1]
    return ids


def create_app():
    """Create Flask app and start the chain monitor for the configured chain set.

    The monitored chains are read from the MONITORED_CHAIN_IDS environment
    variable (comma-separated chain IDs, default "1" for Ethereum mainnet).
    CORS is scoped to the /liquidation/* routes and restricted to the
    RISK_DASHBOARD_URL origin so that /health and /internal/* are not
    world-readable from browsers.
    """
    app = Flask(__name__)

    # Scope CORS to the public-facing /liquidation/* API only, and restrict to
    # the risk-dashboard origin. The /health and /internal/* endpoints are
    # consumed by the sidecar (server-to-server) and do not need CORS headers.
    from urllib.parse import urlparse

    _dashboard_origin = ""
    _dashboard_url = os.environ.get("RISK_DASHBOARD_URL", "")
    if _dashboard_url:
        _parsed = urlparse(_dashboard_url)
        _dashboard_origin = f"{_parsed.scheme}://{_parsed.netloc}" if _parsed.netloc else _dashboard_url
    CORS(app, resources={r"/liquidation/*": {"origins": _dashboard_origin}})

    @app.route("/health", methods=["GET"])
    def health_check():
        chain_manager = getattr(start_monitor, "_chain_manager", None)
        if chain_manager is None:
            return (
                jsonify({"status": "starting", "detail": "ChainManager not yet initialised"}),
                503,
            )

        now = datetime.now(timezone.utc)
        stale = []
        for chain_id, monitor in chain_manager.monitors.items():
            last = getattr(monitor, "_last_activity_at", None)
            if last is None:
                stale.append({"chain": chain_id, "reason": "no activity recorded yet"})
            elif (now - last) > HEALTH_STALE_THRESHOLD:
                stale.append(
                    {
                        "chain": chain_id,
                        "reason": "activity stale",
                        "last_activity": last.isoformat(),
                        "stale_for_seconds": int((now - last).total_seconds()),
                    }
                )

        if stale:
            return jsonify({"status": "unhealthy", "stale_chains": stale}), 503

        return jsonify({"status": "healthy", "chains": list(chain_manager.monitors.keys())}), 200

    @app.route("/internal/observability", methods=["GET"])
    def observability():
        """Read-only operational snapshot per chain, scraped by the metrics
        exporter sidecar and shipped to CloudWatch (Twyne/liquidation-bot).
        Thresholds/alerting live in Grafana, not here."""
        chain_manager = getattr(start_monitor, "_chain_manager", None)
        if chain_manager is None:
            return jsonify({"status": "starting", "chains": {}}), 503

        chains = {}
        for chain_id, monitor in chain_manager.monitors.items():
            try:
                chains[str(chain_id)] = monitor.get_observability_snapshot()
            except Exception as ex:  # never let a metrics scrape take down the bot
                chains[str(chain_id)] = {"error": str(ex)}

        return jsonify({"chains": chains}), 200

    chain_ids = _parse_chain_ids()
    logger.info("Starting monitors for chain IDs: %s", chain_ids)

    # Register graceful-shutdown handlers on the main thread BEFORE spawning the
    # monitor thread, so a SIGTERM/SIGINT during startup is still handled.
    _register_shutdown_handler()

    monitor_thread = threading.Thread(target=start_monitor, args=(chain_ids,))
    monitor_thread.start()

    # Register the rewards blueprint after starting the monitor
    app.register_blueprint(liquidation, url_prefix="/liquidation")

    return app
