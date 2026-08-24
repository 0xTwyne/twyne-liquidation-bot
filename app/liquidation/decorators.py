"""
Decorators and API request utilities.
"""

import functools
import logging
import time
from typing import Any, Callable, Dict, Optional

import requests


def retry_request(logger: logging.Logger, max_retries: int = 3, delay: int = 10) -> Callable:
    """
    Decorator to retry a function on transient RequestException.

    4xx responses (except 429 Too Many Requests) are deterministic client-side
    errors and are NOT retried — retrying them wastes ~20 s per call in the
    hot liquidation path and never succeeds.  429 is retried with a short
    exponential back-off that respects the Retry-After header when present.

    Args:
        logger: Logger instance for retry logging.
        max_retries: Maximum number of retry attempts for retryable errors.
        delay: Base delay between retries in seconds.

    Returns:
        Decorated function with retry logic.
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            for attempt in range(1, max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except requests.HTTPError as e:
                    status = e.response.status_code if e.response is not None else None
                    body = e.response.text if e.response is not None else ""
                    if status is not None and 400 <= status < 500 and status != 429:
                        # Deterministic client error — do not retry.
                        logger.error(
                            "API request failed with %s (not retrying): %s",
                            status,
                            body[:500],
                        )
                        return None
                    # 429 or non-4xx HTTP error — fall through to retry logic below.
                    retry_after = None
                    if status == 429 and e.response is not None:
                        retry_after_header = e.response.headers.get("Retry-After")
                        if retry_after_header is not None:
                            try:
                                retry_after = int(retry_after_header)
                            except ValueError:
                                pass
                    sleep_time = retry_after if retry_after is not None else min(delay * attempt, 60)
                    logger.error(
                        "HTTP %s on API request, waiting %s s before retrying. Attempt %s/%s. Body: %s",
                        status,
                        sleep_time,
                        attempt,
                        max_retries,
                        body[:200],
                    )
                    if attempt == max_retries:
                        logger.error("Failed after %s attempts.", max_retries)
                        return None
                    time.sleep(sleep_time)
                except requests.RequestException as e:
                    logger.error(
                        "Error in API request, waiting %s seconds before retrying. Attempt %s/%s",
                        delay,
                        attempt,
                        max_retries,
                    )
                    logger.error("Error: %s", e)

                    if attempt == max_retries:
                        logger.error("Failed after %s attempts.", max_retries)
                        return None

                    time.sleep(delay)

        return wrapper

    return decorator


@retry_request(logging.getLogger("liquidation_bot"))
def make_api_request(url: str, headers: Dict[str, str], params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Make an API request with retry functionality.

    Args:
        url: The URL for the API request.
        headers: Headers for the request.
        params: Parameters for the request.

    Returns:
        JSON response if successful, None otherwise.
    """
    response = requests.get(url, headers=headers, params=params, timeout=10)
    response.raise_for_status()
    return response.json()
