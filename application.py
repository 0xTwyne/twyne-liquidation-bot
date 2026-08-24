"""
Start point for running flask app
"""
import os

from dotenv import load_dotenv

load_dotenv()

import sentry_sdk
from sentry_sdk.integrations.flask import FlaskIntegration
from sentry_sdk.integrations.logging import LoggingIntegration

from app import create_app


def _before_send(event, hint):
    """Drop known-transient exceptions to protect the Sentry quota.

    Tune in Phase 2 once we have observed real volume — start conservative
    so we don't accidentally hide real issues.
    """
    exc_info = hint.get("exc_info")
    if not exc_info:
        return event

    exc_type = exc_info[0]
    if exc_type is None:
        return event

    exc_module = exc_type.__module__ or ""
    exc_name = exc_type.__name__

    # Transient web3 RPC retry/timeout exhaustion — frequent under load,
    # not actionable individually.
    if exc_module.startswith("web3") and exc_name == "TimeExhausted":
        return None

    # Transient network blips against RPC providers / 1inch / etc.
    if exc_module.startswith("requests") and exc_name in {"ConnectionError", "Timeout", "ReadTimeout"}:
        return None

    return event


_sentry_dsn = os.environ.get("SENTRY_DSN")
if _sentry_dsn:
    sentry_sdk.init(
        dsn=_sentry_dsn,
        environment=os.environ.get("SENTRY_ENVIRONMENT", "production"),
        release=os.environ.get("SENTRY_RELEASE"),
        integrations=[
            FlaskIntegration(),
            # Capture INFO+ logs as breadcrumbs (attached as context to events).
            # event_level=None disables auto-event-from-log so we don't
            # double-count exceptions already captured via other integrations.
            LoggingIntegration(event_level=None),
        ],
        traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0.0")),
        before_send=_before_send,
        send_default_pii=False,
    )

application = create_app()

if __name__ == "__main__":
    application.run(host="0.0.0.0", port=8080, debug=False)
