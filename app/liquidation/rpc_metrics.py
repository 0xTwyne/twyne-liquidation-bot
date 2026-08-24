"""Per-thread eth_call counter for proving the per-tick RPC reduction (DEV-554).

The 32-worker pool shares one Web3 provider, so a *global* counter cannot
attribute calls to a single vault tick. Each tick runs entirely inside one worker
thread (``AccountMonitor._process_account_update`` is submitted to the executor),
so a ``threading.local`` counter attributes ``eth_call`` RPCs to exactly the tick
that issued them, with zero cross-thread contention.

``install_eth_call_counter`` wraps the provider's ``make_request`` once so every
``eth_call`` increments the calling thread's counter. It is fail-open: any problem
installing the wrapper is swallowed (observability must never break the bot).
"""

import threading

_local = threading.local()


def reset_eth_call_count() -> None:
    _local.eth_call_count = 0


def get_eth_call_count() -> int:
    return getattr(_local, "eth_call_count", 0)


def increment_eth_call_count(n: int = 1) -> None:
    _local.eth_call_count = getattr(_local, "eth_call_count", 0) + n


def install_eth_call_counter(w3) -> bool:
    """Idempotently wrap ``w3.provider.make_request`` to count ``eth_call`` RPCs.

    Returns True if a real provider counter is now installed, False otherwise
    (e.g. a mock provider, or already installed). Never raises.
    """
    try:
        provider = getattr(w3, "provider", None)
        if provider is None:
            return False
        if getattr(provider, "_twyne_eth_call_counter_installed", False):
            return False
        original = provider.make_request

        def counting_make_request(method, params):
            if method == "eth_call":
                increment_eth_call_count()
            return original(method, params)

        provider.make_request = counting_make_request
        provider._twyne_eth_call_counter_installed = True
        return True
    except Exception:
        # Fail open — the counter is observability only.
        return False
