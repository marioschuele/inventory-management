from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
import math
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Optional
from datetime import datetime, timedelta
from pydantic import BaseModel, Field
import itertools
from mock_data import inventory_items, orders, demand_forecasts, backlog_items, spending_summary, monthly_spending, category_spending, recent_transactions, purchase_orders

app = FastAPI(title="Factory Inventory Management System")

# Quarter mapping for date filtering
QUARTER_MAP = {
    'Q1-2025': ['2025-01', '2025-02', '2025-03'],
    'Q2-2025': ['2025-04', '2025-05', '2025-06'],
    'Q3-2025': ['2025-07', '2025-08', '2025-09'],
    'Q4-2025': ['2025-10', '2025-11', '2025-12']
}

def filter_by_month(items: list, month: Optional[str]) -> list:
    """Filter items by month/quarter based on order_date field"""
    if not month or month == 'all':
        return items

    if month.startswith('Q'):
        # Handle quarters
        if month in QUARTER_MAP:
            months = QUARTER_MAP[month]
            return [item for item in items if any(m in item.get('order_date', '') for m in months)]
    else:
        # Direct month match
        return [item for item in items if month in item.get('order_date', '')]

    return items

def apply_filters(items: list, warehouse: Optional[str] = None, category: Optional[str] = None,
                 status: Optional[str] = None) -> list:
    """Apply common filters to a list of items"""
    filtered = items

    if warehouse and warehouse != 'all':
        filtered = [item for item in filtered if item.get('warehouse') == warehouse]

    if category and category != 'all':
        filtered = [item for item in filtered if item.get('category', '').lower() == category.lower()]

    if status and status != 'all':
        filtered = [item for item in filtered if item.get('status', '').lower() == status.lower()]

    return filtered

def _json_safe(value):
    """Replace non-finite floats (NaN/Infinity), which strict JSON can't encode, with strings."""
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Same 422 body as FastAPI's default, but safe when the rejected input was NaN/Infinity
    (the default handler fails to serialize those and returns a 500 instead)."""
    return JSONResponse(status_code=422, content={"detail": _json_safe(jsonable_encoder(exc.errors()))})

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Data models
class InventoryItem(BaseModel):
    id: str
    sku: str
    name: str
    category: str
    warehouse: str
    quantity_on_hand: int
    reorder_point: int
    unit_cost: float
    location: str
    last_updated: str

class Order(BaseModel):
    id: str
    order_number: str
    customer: str
    items: List[dict]
    status: str
    order_date: str
    expected_delivery: str
    total_value: float
    actual_delivery: Optional[str] = None
    warehouse: Optional[str] = None
    category: Optional[str] = None

class DemandForecast(BaseModel):
    id: str
    item_sku: str
    item_name: str
    current_demand: int
    forecasted_demand: int
    trend: str
    period: str
    category: str
    unit_cost: float
    quantity_on_hand: int

class BacklogItem(BaseModel):
    id: str
    order_id: str
    item_sku: str
    item_name: str
    quantity_needed: int
    quantity_available: int
    days_delayed: int
    priority: str
    has_purchase_order: Optional[bool] = False

class PurchaseOrder(BaseModel):
    id: str
    backlog_item_id: str
    supplier_name: str
    quantity: int
    unit_cost: float
    expected_delivery_date: str
    status: str
    created_date: str
    notes: Optional[str] = None

class CreatePurchaseOrderRequest(BaseModel):
    backlog_item_id: str
    supplier_name: str
    quantity: int
    unit_cost: float
    expected_delivery_date: str
    notes: Optional[str] = None

# Restocking
# Supplier lead time (days) per category. An order's lead time is the longest
# lead time of any category it contains, since the order is delivered as one shipment.
CATEGORY_LEAD_TIME_DAYS = {
    'power supplies': 7,
    'sensors': 10,
    'circuit boards': 14,
    'controllers': 18,
    'actuators': 21,
}
DEFAULT_LEAD_TIME_DAYS = 14

# Submitted restocking orders live only in memory (reset on server restart).
# Kept separate from the mock `orders` list so existing order stats are unaffected.
restock_orders: List[dict] = []
# Monotonic order sequence; next() on itertools.count is atomic under the GIL, so
# concurrent requests (sync endpoints run in a threadpool) never share an id.
_restock_order_seq = itertools.count(1)

def get_lead_time_days(category: str) -> int:
    return CATEGORY_LEAD_TIME_DAYS.get((category or '').lower(), DEFAULT_LEAD_TIME_DAYS)

def to_cents(amount: float) -> int:
    """Money is compared in integer cents (same as the client) to avoid float drift."""
    return int(round(amount * 100))

def get_on_order_quantities() -> dict:
    """Units already ordered per SKU via submitted restock orders (incoming stock)."""
    on_order: dict = {}
    for order in restock_orders:
        if order['status'] != 'Submitted':
            continue
        for item in order['items']:
            on_order[item['sku']] = on_order.get(item['sku'], 0) + item['quantity']
    return on_order

class RestockRecommendation(BaseModel):
    sku: str
    name: str
    category: str
    quantity_on_hand: int
    quantity_on_order: int
    forecasted_demand: int
    shortfall: int
    unit_cost: float
    line_cost: float
    lead_time_days: int
    trend: str

class RestockOrderLine(BaseModel):
    sku: str
    quantity: int = Field(gt=0)

class CreateRestockOrderRequest(BaseModel):
    items: List[RestockOrderLine]
    # allow_inf_nan=False: JSON NaN/Infinity would otherwise slip past the budget comparison
    budget: float = Field(gt=0, allow_inf_nan=False)

class RestockOrderItem(BaseModel):
    sku: str
    name: str
    category: str
    quantity: int
    unit_price: float
    line_total: float

class RestockOrder(BaseModel):
    id: str
    order_number: str
    items: List[RestockOrderItem]
    total_value: float
    budget: float
    status: str
    order_date: str
    lead_time_days: int
    expected_delivery: str

def build_restock_recommendations() -> List[dict]:
    """Forecast items whose on-hand plus on-order stock is below forecasted demand,
    highest priority first."""
    on_order = get_on_order_quantities()
    recs = []
    for f in demand_forecasts:
        if f['forecasted_demand'] <= 0:
            continue
        # Stock already ordered counts as incoming, so a placed order shrinks (or removes)
        # the recommendation and the same shortfall can't be ordered twice.
        ordered = on_order.get(f['item_sku'], 0)
        shortfall = f['forecasted_demand'] - f['quantity_on_hand'] - ordered
        if shortfall <= 0:
            continue
        recs.append({
            'sku': f['item_sku'],
            'name': f['item_name'],
            'category': f['category'],
            'quantity_on_hand': f['quantity_on_hand'],
            'quantity_on_order': ordered,
            'forecasted_demand': f['forecasted_demand'],
            'shortfall': shortfall,
            'unit_cost': f['unit_cost'],
            'line_cost': shortfall * to_cents(f['unit_cost']) / 100,
            'lead_time_days': get_lead_time_days(f['category']),
            'trend': f['trend'],
        })
    # Priority = shortfall as a share of forecasted demand (coverage gap), not raw units,
    # so high-volume cheap items don't always outrank critical low-volume ones.
    # Ties are broken in favour of items with increasing demand.
    recs.sort(key=lambda r: (r['shortfall'] / r['forecasted_demand'], r['trend'] == 'increasing'), reverse=True)
    return recs

# API endpoints
@app.get("/")
def root():
    return {"message": "Factory Inventory Management System API", "version": "1.0.0"}

@app.get("/api/inventory", response_model=List[InventoryItem])
def get_inventory(
    warehouse: Optional[str] = None,
    category: Optional[str] = None
):
    """Get all inventory items with optional filtering"""
    return apply_filters(inventory_items, warehouse, category)

@app.get("/api/inventory/{item_id}", response_model=InventoryItem)
def get_inventory_item(item_id: str):
    """Get a specific inventory item"""
    item = next((item for item in inventory_items if item["id"] == item_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    return item

@app.get("/api/orders", response_model=List[Order])
def get_orders(
    warehouse: Optional[str] = None,
    category: Optional[str] = None,
    status: Optional[str] = None,
    month: Optional[str] = None
):
    """Get all orders with optional filtering"""
    filtered_orders = apply_filters(orders, warehouse, category, status)
    filtered_orders = filter_by_month(filtered_orders, month)
    return filtered_orders

@app.get("/api/orders/{order_id}", response_model=Order)
def get_order(order_id: str):
    """Get a specific order"""
    order = next((order for order in orders if order["id"] == order_id), None)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order

@app.get("/api/demand", response_model=List[DemandForecast])
def get_demand_forecasts():
    """Get demand forecasts"""
    return demand_forecasts

@app.get("/api/restocking/recommendations", response_model=List[RestockRecommendation])
def get_restock_recommendations():
    """Get demand-forecast items that need restocking, ordered by priority"""
    return build_restock_recommendations()

@app.get("/api/restocking/orders", response_model=List[RestockOrder])
def get_restock_orders():
    """Get submitted restocking orders, newest first"""
    return list(reversed(restock_orders))

@app.post("/api/restocking/orders", response_model=RestockOrder, status_code=201)
def create_restock_order(request: CreateRestockOrderRequest):
    """Submit a restocking order. Prices and totals are recomputed server-side."""
    if not request.items:
        raise HTTPException(status_code=400, detail="Order must contain at least one item")

    recs_by_sku = {r['sku']: r for r in build_restock_recommendations()}
    seen = set()
    items = []
    for line in request.items:
        rec = recs_by_sku.get(line.sku)
        if not rec:
            raise HTTPException(status_code=400, detail=f"SKU {line.sku} is not eligible for restocking")
        if line.sku in seen:
            raise HTTPException(status_code=400, detail=f"SKU {line.sku} appears more than once")
        seen.add(line.sku)
        # Quantity > 0 is enforced by the model; cap at the remaining shortfall
        # (which already subtracts units on earlier submitted orders).
        if line.quantity > rec['shortfall']:
            raise HTTPException(
                status_code=400,
                detail=f"Quantity for {line.sku} must be between 1 and {rec['shortfall']}"
            )
        line_cents = line.quantity * to_cents(rec['unit_cost'])
        items.append({
            'sku': rec['sku'],
            'name': rec['name'],
            'category': rec['category'],
            'quantity': line.quantity,
            'unit_price': rec['unit_cost'],
            'line_cents': line_cents,
        })

    # Integer-cent comparison mirrors the client's allocation math exactly
    total_cents = sum(i['line_cents'] for i in items)
    if total_cents > to_cents(request.budget):
        raise HTTPException(
            status_code=400,
            detail=f"Order total {total_cents / 100:.2f} exceeds budget {request.budget:.2f}"
        )
    for item in items:
        item['line_total'] = item.pop('line_cents') / 100

    lead_time_days = max(get_lead_time_days(i['category']) for i in items)
    order_date = datetime.now().replace(microsecond=0)
    seq = next(_restock_order_seq)
    order = {
        'id': f"RST-{seq}",
        'order_number': f"RST-{order_date.year}-{seq:04d}",
        'items': items,
        'total_value': total_cents / 100,
        'budget': request.budget,
        'status': 'Submitted',
        'order_date': order_date.isoformat(),
        'lead_time_days': lead_time_days,
        'expected_delivery': (order_date + timedelta(days=lead_time_days)).isoformat(),
    }
    restock_orders.append(order)
    return order

@app.get("/api/backlog", response_model=List[BacklogItem])
def get_backlog():
    """Get backlog items with purchase order status"""
    # Add has_purchase_order flag to each backlog item
    result = []
    for item in backlog_items:
        item_dict = dict(item)
        # Check if this backlog item has a purchase order
        has_po = any(po["backlog_item_id"] == item["id"] for po in purchase_orders)
        item_dict["has_purchase_order"] = has_po
        result.append(item_dict)
    return result

@app.get("/api/dashboard/summary")
def get_dashboard_summary(
    warehouse: Optional[str] = None,
    category: Optional[str] = None,
    status: Optional[str] = None,
    month: Optional[str] = None
):
    """Get summary statistics for dashboard with optional filtering"""
    # Filter inventory
    filtered_inventory = apply_filters(inventory_items, warehouse, category)

    # Filter orders
    filtered_orders = apply_filters(orders, warehouse, category, status)
    filtered_orders = filter_by_month(filtered_orders, month)

    total_inventory_value = sum(item["quantity_on_hand"] * item["unit_cost"] for item in filtered_inventory)
    low_stock_items = len([item for item in filtered_inventory if item["quantity_on_hand"] <= item["reorder_point"]])
    pending_orders = len([order for order in filtered_orders if order["status"] in ["Processing", "Backordered"]])
    total_backlog_items = len(backlog_items)

    return {
        "total_inventory_value": round(total_inventory_value, 2),
        "low_stock_items": low_stock_items,
        "pending_orders": pending_orders,
        "total_backlog_items": total_backlog_items,
        "total_orders_value": sum(order["total_value"] for order in filtered_orders)
    }

@app.get("/api/spending/summary")
def get_spending_summary():
    """Get spending summary statistics"""
    return spending_summary

@app.get("/api/spending/monthly")
def get_monthly_spending():
    """Get monthly spending breakdown"""
    return monthly_spending

@app.get("/api/spending/categories")
def get_category_spending():
    """Get spending by category"""
    return category_spending

@app.get("/api/spending/transactions")
def get_recent_transactions():
    """Get recent transactions"""
    return recent_transactions

@app.get("/api/reports/quarterly")
def get_quarterly_reports():
    """Get quarterly performance reports"""
    # Calculate quarterly statistics from orders
    quarters = {}

    for order in orders:
        order_date = order.get('order_date', '')
        # Determine quarter
        if '2025-01' in order_date or '2025-02' in order_date or '2025-03' in order_date:
            quarter = 'Q1-2025'
        elif '2025-04' in order_date or '2025-05' in order_date or '2025-06' in order_date:
            quarter = 'Q2-2025'
        elif '2025-07' in order_date or '2025-08' in order_date or '2025-09' in order_date:
            quarter = 'Q3-2025'
        elif '2025-10' in order_date or '2025-11' in order_date or '2025-12' in order_date:
            quarter = 'Q4-2025'
        else:
            continue

        if quarter not in quarters:
            quarters[quarter] = {
                'quarter': quarter,
                'total_orders': 0,
                'total_revenue': 0,
                'delivered_orders': 0,
                'avg_order_value': 0
            }

        quarters[quarter]['total_orders'] += 1
        quarters[quarter]['total_revenue'] += order.get('total_value', 0)
        if order.get('status') == 'Delivered':
            quarters[quarter]['delivered_orders'] += 1

    # Calculate averages and fulfillment rate
    result = []
    for q, data in quarters.items():
        if data['total_orders'] > 0:
            data['avg_order_value'] = round(data['total_revenue'] / data['total_orders'], 2)
            data['fulfillment_rate'] = round((data['delivered_orders'] / data['total_orders']) * 100, 1)
        result.append(data)

    # Sort by quarter
    result.sort(key=lambda x: x['quarter'])
    return result

@app.get("/api/reports/monthly-trends")
def get_monthly_trends():
    """Get month-over-month trends"""
    months = {}

    for order in orders:
        order_date = order.get('order_date', '')
        if not order_date:
            continue

        # Extract month (format: YYYY-MM-DD)
        month = order_date[:7]  # Gets YYYY-MM

        if month not in months:
            months[month] = {
                'month': month,
                'order_count': 0,
                'revenue': 0,
                'delivered_count': 0
            }

        months[month]['order_count'] += 1
        months[month]['revenue'] += order.get('total_value', 0)
        if order.get('status') == 'Delivered':
            months[month]['delivered_count'] += 1

    # Convert to list and sort
    result = list(months.values())
    result.sort(key=lambda x: x['month'])
    return result

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
