from datetime import date
from unittest.mock import Mock, patch

import pytest

from src.robot.etf.ishares import ISharesETFFetcher


def _product_data_payload():
    return {
        "componentsByNameMap": {
            "holdings": {
                "dataPointsByNameMap": {
                    "asOfDate": {"value": 20260731},
                },
                "containersByNameMap": {
                    "all": {
                        "dataPointsByNameMap": {
                            "ticker": {"value": ["NVDA"]},
                            "issueName": {"value": ["NVIDIA CORP"]},
                            "assetClass": {"value": ["Equity"]},
                            "holdingPercent": {"value": [8.69]},
                            "marketValue": {"value": [3861477642.0]},
                            "unitPrice": {"value": [178.0]},
                            "unitsHeld": {"value": [21693694]},
                            "exchange": {"value": ["NASDAQ"]},
                        }
                    }
                },
            }
        }
    }


def test_product_data_uses_component_as_of_date_when_rows_lack_date():
    response = Mock()
    response.json.return_value = _product_data_payload()
    response.raise_for_status.return_value = None

    with patch("src.robot.etf.ishares.requests.get", return_value=response):
        holdings = ISharesETFFetcher()._get_holdings_from_product_data("SOXX.US")

    assert holdings.update_date == date(2026, 7, 31)
    assert len(holdings.holdings) == 1
    assert holdings.holdings[0].symbol == "NVDA.US"
    assert holdings.total_weight == pytest.approx(0.0869)
