import os

from flask import jsonify, make_response, request
from flask_restx import Namespace, Resource
from marshmallow import ValidationError

from limiter import limiter
from services.tradebook_consolidation_service import get_tradebook_analysis
from utils.logging import get_logger

from .account_schema import TradebookSchema

API_RATE_LIMIT = os.getenv("API_RATE_LIMIT", "10 per second")
api = Namespace("tradebookanalysis", description="Consolidated round-trip + ledger analysis")

logger = get_logger(__name__)
tradebook_schema = TradebookSchema()


@api.route("/", strict_slashes=False)
class TradebookAnalysis(Resource):
    @limiter.limit(API_RATE_LIMIT)
    def post(self):
        """Round-trip consolidation (with model fees) + broker ledger P&L."""
        try:
            data = tradebook_schema.load(request.json)
            success, response_data, status_code = get_tradebook_analysis(api_key=data["apikey"])
            return make_response(jsonify(response_data), status_code)
        except ValidationError as err:
            return make_response(jsonify({"status": "error", "message": err.messages}), 400)
        except Exception as e:
            logger.exception(f"Unexpected error in tradebookanalysis endpoint: {e}")
            return make_response(
                jsonify({"status": "error", "message": "An unexpected error occurred"}), 500
            )
