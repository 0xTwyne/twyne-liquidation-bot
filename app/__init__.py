"""
Creates and returns main flask app
"""

import threading

from flask import Flask, jsonify
from flask_cors import CORS

from .liquidation.routes import liquidation, start_monitor


def create_app():
    """Create Flask app with specified chain IDs"""
    app = Flask(__name__)
    CORS(app)

    @app.route("/health", methods=["GET"])
    def health_check():
        return jsonify({"status": "healthy"}), 200

    chain_ids = [1, 8453]

    monitor_thread = threading.Thread(target=start_monitor, args=(chain_ids,))
    monitor_thread.start()

    # Register the rewards blueprint after starting the monitor
    app.register_blueprint(liquidation, url_prefix="/liquidation")

    return app
