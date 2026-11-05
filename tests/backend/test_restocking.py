"""
Tests for restocking API endpoints.
"""
import pytest

import main


@pytest.fixture(autouse=True)
def reset_restock_orders():
    """Submitted restock orders are kept in memory; clear them so tests stay independent."""
    main.restock_orders.clear()
    yield
    main.restock_orders.clear()


def _recommendations(client):
    response = client.get("/api/restocking/recommendations")
    assert response.status_code == 200
    return response.json()


class TestRestockingRecommendations:
    """Test suite for restock recommendations."""

    def test_get_restock_recommendations(self, client):
        """Test recommendations structure."""
        data = _recommendations(client)
        assert isinstance(data, list)
        assert len(data) > 0

        for rec in data:
            for field in ["sku", "name", "category", "quantity_on_hand", "quantity_on_order",
                          "forecasted_demand", "shortfall", "unit_cost", "line_cost",
                          "lead_time_days", "trend"]:
                assert field in rec
            assert rec["shortfall"] == (
                rec["forecasted_demand"] - rec["quantity_on_hand"] - rec["quantity_on_order"]
            )
            assert rec["shortfall"] > 0
            assert abs(rec["line_cost"] - rec["shortfall"] * rec["unit_cost"]) < 0.01
            assert rec["lead_time_days"] > 0

    def test_recommendations_exclude_fully_stocked_items(self, client):
        """Items with stock at or above forecast are not recommended."""
        demand = client.get("/api/demand").json()
        stocked = {f["item_sku"] for f in demand if f["quantity_on_hand"] >= f["forecasted_demand"]}
        rec_skus = {r["sku"] for r in _recommendations(client)}
        assert stocked.isdisjoint(rec_skus)

    def test_recommendations_sorted_by_coverage_gap(self, client):
        """Recommendations are ordered by shortfall share of forecast, highest first."""
        data = _recommendations(client)
        gaps = [r["shortfall"] / r["forecasted_demand"] for r in data]
        assert gaps == sorted(gaps, reverse=True)


class TestRestockingOrders:
    """Test suite for submitting and listing restock orders."""

    def test_create_restock_order(self, client):
        """Test placing an order with server-side totals and lead time."""
        recs = _recommendations(client)[:2]
        payload = {
            "items": [{"sku": r["sku"], "quantity": r["shortfall"]} for r in recs],
            "budget": 1_000_000,
        }
        response = client.post("/api/restocking/orders", json=payload)
        assert response.status_code == 201

        order = response.json()
        assert order["status"] == "Submitted"
        assert order["order_number"].startswith("RST-")
        assert len(order["items"]) == 2
        expected_total = sum(r["line_cost"] for r in recs)
        assert abs(order["total_value"] - expected_total) < 0.01
        assert order["lead_time_days"] == max(r["lead_time_days"] for r in recs)
        assert "T" in order["order_date"]
        assert order["expected_delivery"] > order["order_date"]

    def test_get_restock_orders_newest_first(self, client):
        """Submitted orders are listed newest first."""
        rec = _recommendations(client)[0]
        for qty in (1, 2):
            response = client.post("/api/restocking/orders", json={
                "items": [{"sku": rec["sku"], "quantity": qty}], "budget": 1_000_000})
            assert response.status_code == 201

        response = client.get("/api/restocking/orders")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        assert data[0]["items"][0]["quantity"] == 2
        assert data[1]["items"][0]["quantity"] == 1

    def test_create_restock_order_over_budget(self, client):
        """Orders whose total exceeds the budget are rejected."""
        rec = _recommendations(client)[0]
        response = client.post("/api/restocking/orders", json={
            "items": [{"sku": rec["sku"], "quantity": rec["shortfall"]}],
            "budget": rec["line_cost"] - 0.01,
        })
        assert response.status_code == 400
        assert "budget" in response.json()["detail"].lower()

    def test_create_restock_order_exact_budget(self, client):
        """An order whose total equals the budget exactly is accepted (client/server agree)."""
        rec = _recommendations(client)[0]
        response = client.post("/api/restocking/orders", json={
            "items": [{"sku": rec["sku"], "quantity": rec["shortfall"]}],
            "budget": rec["line_cost"],
        })
        assert response.status_code == 201
        assert abs(response.json()["total_value"] - rec["line_cost"]) < 0.01

    def test_submitted_order_reduces_shortfall(self, client):
        """Units already on order count as incoming stock, so they can't be ordered twice."""
        recs = _recommendations(client)
        full, partial = recs[0], recs[1]
        response = client.post("/api/restocking/orders", json={
            "items": [
                {"sku": full["sku"], "quantity": full["shortfall"]},
                {"sku": partial["sku"], "quantity": 1},
            ],
            "budget": 1_000_000,
        })
        assert response.status_code == 201

        after = {r["sku"]: r for r in _recommendations(client)}
        assert full["sku"] not in after
        assert after[partial["sku"]]["quantity_on_order"] == 1
        assert after[partial["sku"]]["shortfall"] == partial["shortfall"] - 1

        # Re-ordering the fully covered SKU is rejected
        response = client.post("/api/restocking/orders", json={
            "items": [{"sku": full["sku"], "quantity": 1}], "budget": 1_000_000})
        assert response.status_code == 400

    def test_create_restock_order_invalid_values(self, client):
        """Non-positive quantity/budget and NaN budget fail model validation (422)."""
        sku = _recommendations(client)[0]["sku"]
        bad_payloads = [
            f'{{"items": [{{"sku": "{sku}", "quantity": 0}}], "budget": 1000}}',
            f'{{"items": [{{"sku": "{sku}", "quantity": 1}}], "budget": 0}}',
            f'{{"items": [{{"sku": "{sku}", "quantity": 1}}], "budget": NaN}}',
            f'{{"items": [{{"sku": "{sku}", "quantity": 1}}], "budget": Infinity}}',
        ]
        for body in bad_payloads:
            response = client.post("/api/restocking/orders", content=body,
                                   headers={"Content-Type": "application/json"})
            assert response.status_code == 422, body
        assert client.get("/api/restocking/orders").json() == []

    def test_create_restock_order_duplicate_sku(self, client):
        """The same SKU may appear only once per order."""
        sku = _recommendations(client)[0]["sku"]
        response = client.post("/api/restocking/orders", json={
            "items": [{"sku": sku, "quantity": 1}, {"sku": sku, "quantity": 1}],
            "budget": 1_000_000,
        })
        assert response.status_code == 400

    def test_create_restock_order_unknown_sku(self, client):
        """Unknown SKUs are rejected."""
        response = client.post("/api/restocking/orders", json={
            "items": [{"sku": "NOPE-999", "quantity": 1}], "budget": 1000})
        assert response.status_code == 400

    def test_create_restock_order_empty(self, client):
        """Orders with no items are rejected."""
        response = client.post("/api/restocking/orders", json={"items": [], "budget": 1000})
        assert response.status_code == 400

    def test_create_restock_order_quantity_above_shortfall(self, client):
        """Quantities above the shortfall are rejected."""
        rec = _recommendations(client)[0]
        response = client.post("/api/restocking/orders", json={
            "items": [{"sku": rec["sku"], "quantity": rec["shortfall"] + 1}],
            "budget": 1_000_000,
        })
        assert response.status_code == 400

    def test_restock_orders_do_not_change_mock_orders(self, client):
        """Restock orders are kept separate from the regular orders list."""
        before = len(client.get("/api/orders").json())
        rec = _recommendations(client)[0]
        client.post("/api/restocking/orders", json={
            "items": [{"sku": rec["sku"], "quantity": 1}], "budget": 1_000_000})
        assert len(client.get("/api/orders").json()) == before
