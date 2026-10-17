"""
Seed test orders: fills a Shopify DEVELOPMENT store with backdated, paid test orders
so the sales check has a realistic week of history to compare against.

Needs the app scope  write_orders  (add it in the Dev Dashboard, release a new
version, and approve the update in your store admin).

Usage:
  python seed_orders.py --dry-run          # show what would be created, create nothing
  python seed_orders.py                    # 10 days x 3 orders per day
  python seed_orders.py --days 14 --per-day 4

Only run this against a development store. Shopify limits how fast dev stores can
create orders, so the script paces itself (about 5 orders per minute).
"""

import argparse
import random
import time
from datetime import datetime, timedelta, timezone

from sales_alerts import API_VERSION, get_access_token, load_env, post_json

PRODUCTS = [
    ("Linen Tote Bag", 34.00),
    ("Ceramic Mug", 22.00),
    ("Soy Candle", 28.00),
    ("Leather Card Holder", 45.00),
    ("Cotton Throw Blanket", 89.00),
    ("Stoneware Bowl Set", 64.00),
]

SHOP_QUERY = "{ shop { currencyCode } }"

CREATE_MUTATION = """
mutation CreateOrder($order: OrderCreateOrderInput!, $options: OrderCreateOptionsInput) {
  orderCreate(order: $order, options: $options) {
    order { name processedAt totalPriceSet { shopMoney { amount } } }
    userErrors { field message }
  }
}
"""


def graphql(cfg, token, query, variables=None):
    return post_json(
        f"https://{cfg['store']}/admin/api/{API_VERSION}/graphql.json",
        {"query": query, "variables": variables or {}},
        {"X-Shopify-Access-Token": token},
    )


def build_orders(days, per_day, currency, now):
    """Plan the orders: random products at random times on each of the past `days` days."""
    plan = []
    for day in range(days, 0, -1):
        for _ in range(per_day):
            when = now - timedelta(days=day, hours=random.randint(0, 20), minutes=random.randint(0, 59))
            items = random.sample(PRODUCTS, k=random.randint(1, 2))
            line_items = [
                {
                    "title": title,
                    "quantity": random.randint(1, 2),
                    "priceSet": {"shopMoney": {"amount": f"{price:.2f}", "currencyCode": currency}},
                }
                for title, price in items
            ]
            total = sum(float(li["priceSet"]["shopMoney"]["amount"]) * li["quantity"] for li in line_items)
            plan.append({"when": when, "line_items": line_items, "total": total})
    return plan


def create_order(cfg, token, order, currency):
    variables = {
        "order": {
            "processedAt": order["when"].isoformat(),
            "financialStatus": "PAID",
            "lineItems": order["line_items"],
            "transactions": [
                {
                    "kind": "SALE",
                    "status": "SUCCESS",
                    "processedAt": order["when"].isoformat(),
                    "amountSet": {"shopMoney": {"amount": f"{order['total']:.2f}", "currencyCode": currency}},
                }
            ],
            "tags": ["seed-test-order"],
        },
        "options": {"sendReceipt": False, "sendFulfillmentReceipt": False, "inventoryBehaviour": "BYPASS"},
    }
    for attempt in range(3):
        result = graphql(cfg, token, CREATE_MUTATION, variables)
        errors = result.get("errors")
        if errors and any("throttl" in str(e).lower() or "limit" in str(e).lower() for e in errors):
            print("  Rate limited by Shopify, waiting 60s...")
            time.sleep(60)
            continue
        if errors:
            raise RuntimeError(f"Shopify API error: {str(errors)[:300]}")
        payload = result["data"]["orderCreate"]
        if payload["userErrors"]:
            raise RuntimeError(f"Order rejected: {payload['userErrors']}")
        return payload["order"]
    raise RuntimeError("Gave up after repeated rate limits. Try again in a few minutes.")


def main():
    parser = argparse.ArgumentParser(description="Create backdated test orders in a dev store.")
    parser.add_argument("--days", type=int, default=10, help="how many past days to fill (default 10)")
    parser.add_argument("--per-day", type=int, default=3, help="orders per day (default 3)")
    parser.add_argument("--dry-run", action="store_true", help="print the plan without creating anything")
    args = parser.parse_args()

    cfg = load_env()
    token = get_access_token(cfg)
    currency = graphql(cfg, token, SHOP_QUERY)["data"]["shop"]["currencyCode"]
    plan = build_orders(args.days, args.per_day, currency, datetime.now(timezone.utc))

    print(f"Planning {len(plan)} test orders over {args.days} days in {cfg['store']} ({currency}).")
    if args.dry_run:
        for o in plan:
            print(f"  {o['when']:%Y-%m-%d %H:%M}  {o['total']:8.2f} {currency}")
        print("Dry run: nothing created.")
        return

    for i, o in enumerate(plan, 1):
        created = create_order(cfg, token, o, currency)
        print(f"  [{i}/{len(plan)}] {created['name']}  {o['when']:%Y-%m-%d}  {o['total']:.2f} {currency}")
        if i < len(plan):
            time.sleep(12)  # stay under the dev-store limit of about 5 orders per minute
    print("Done. Run  python sales_alerts.py  to see the new baseline.")


if __name__ == "__main__":
    main()
