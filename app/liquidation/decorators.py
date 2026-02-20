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
    Decorator to retry a function on RequestException.

    Args:
        logger: Logger instance for retry logging.
        max_retries: Maximum number of retry attempts.
        delay: Delay between retries in seconds.

    Returns:
        Decorated function with retry logic.
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            for attempt in range(1, max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except requests.RequestException as e:
                    logger.error(
                        "Error in API request, waiting %s seconds before retrying. Attempt %s/%s",
                        delay, attempt, max_retries,
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
