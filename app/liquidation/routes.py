"""Module for handling API routes"""

import math

from flask import Blueprint, jsonify, make_response, request

from .bot_manager import ChainManager
from .logging_config import setup_logger

logger = setup_logger()

liquidation = Blueprint("liquidation", __name__)


def start_monitor(chain_ids=None):
    """Start monitoring for specified chains, defaults to Base if none specified"""
    if chain_ids is None:
        chain_ids = [8453]

    chain_manager = ChainManager(chain_ids, notify=True)

    # Store on module level for route access before app context is available
    start_monitor._chain_manager = chain_manager

    chain_manager.start()

    return chain_manager


def _get_chain_manager():
    """Get the chain manager instance."""
    return getattr(start_monitor, "_chain_manager", None)


@liquidation.route("/allPositions", methods=["GET"])
def get_all_positions():
    chain_id = int(request.args.get("chainId", 8453))
    chain_manager = _get_chain_manager()

    if not chain_manager or chain_id not in chain_manager.monitors:
        return jsonify({"error": f"Monitor not initialized for chain {chain_id}"}), 500

    logger.info("API: Getting all positions for chain %s", chain_id)
    monitor = chain_manager.monitors[chain_id]
    sorted_accounts = monitor.get_accounts_by_health_score()

    response = []
    for address, internal_hs, external_hs, balance, internal_borrowed, external_borrowed, symbol in sorted_accounts:
        health_score = min(internal_hs, external_hs)
        if math.isinf(health_score):
            continue
        response.append(
            {
                "account_address": address,
                "internal_health_score": internal_hs,
                "external_health_score": external_hs,
                "health_score": health_score,
                "balance": balance,
                "internal_value_borrowed": internal_borrowed,
                "external_value_borrowed": external_borrowed,
                "symbol": symbol,
            }
        )

    return make_response(jsonify(response))
