"""
Sales Alerts: daily Shopify sales check that posts to Slack.

What it does:
  1. Logs in to Shopify with your app's Client ID + Client Secret
     (client credentials grant, gets a fresh token each run).
  2. Pulls orders processed in the last 8 days.
  3. Compares the last 24 hours to the average day of the 7 days before.
  4. Posts a summary to Slack, flagging a sales drop, a sales spike,
     or a high refund rate.

Setup: put your values in a file called .env next to this script
(see .env.example). Run with:  python sales_alerts.py
No extra packages needed: only Python's standard library.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------- Settings you can tune ----------
API_VERSION = "2026-07"
DROP_THRESHOLD = 0.60    # alert if last 24h sales < 60% of the daily average
SPIKE_THRESHOLD = 1.50   # alert if last 24h sales > 150% of the daily average
REFUND_RATE_ALERT = 0.10 # alert if refunds are more than 10% of sales
# -------------------------------------------

try:
    sys.stdout.reconfigure(encoding="utf-8")  # so Windows terminals print cleanly
except Exception:
    pass


def load_env():
    """Read .env next to this script. Real environment variables win (used later on GitHub)."""
    env_file = Path(__file__).with_name(".env")
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

    needed = ["SHOPIFY_STORE", "SHOPIFY_CLIENT_ID", "SHOPIFY_CLIENT_SECRET", "SLACK_WEBHOOK_URL"]
    missing = [k for k in needed if not os.environ.get(k)]
    if missing:
        sys.exit(f"Missing settings: {', '.join(missing)}. Add them to your .env file.")

    store = os.environ["SHOPIFY_STORE"].replace("https://", "").strip("/")
    if not store.endswith(".myshopify.com"):
        store += ".myshopify.com"
    return {
        "store": store,
        "client_id": os.environ["SHOPIFY_CLIENT_ID"],
        "client_secret": os.environ["SHOPIFY_CLIENT_SECRET"],
        "slack": os.environ["SLACK_WEBHOOK_URL"],
    }


def post_json(url, payload, headers=None):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body.strip().startswith(("{", "[")) else body
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"{e.code} error from {url.split('?')[0]}: {detail}") from None


def get_access_token(cfg):
    """Exchange Client ID + Secret for a short-lived Admin API token."""
    result = post_json(
        f"https://{cfg['store']}/admin/oauth/access_token",
        {
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "grant_type": "client_credentials",
        },
    )
    token = result.get("access_token") if isinstance(result, dict) else None
    if not token:
        raise RuntimeError(f"Shopify did not return a token. Response: {str(result)[:300]}")
    return token


ORDERS_QUERY = """
query Orders($cursor: String, $q: String) {
  orders(first: 100, after: $cursor, query: $q, sortKey: PROCESSED_AT) {
    pageInfo { hasNextPage endCursor }
    nodes {
      name
      processedAt
      totalPriceSet { shopMoney { amount currencyCode } }
      totalRefundedSet { shopMoney { amount } }
    }
  }
}
"""


def fetch_orders(cfg, token, since):
    url = f"https://{cfg['store']}/admin/api/{API_VERSION}/graphql.json"
    headers = {"X-Shopify-Access-Token": token}
    orders, cursor = [], None
    while True:
        result = post_json(
            url,
            {"query": ORDERS_QUERY, "variables": {"cursor": cursor, "q": f"processed_at:>={since.isoformat()}"}},
            headers,
        )
        if result.get("errors"):
            raise RuntimeError(f"Shopify API error: {json.dumps(result['errors'])[:300]}")
        page = result["data"]["orders"]
        orders.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            return orders
        cursor = page["pageInfo"]["endCursor"]


def analyze(orders, now):
    last_24h_start = now - timedelta(hours=24)
    baseline_start = now - timedelta(days=8)
    currency = "USD"
    recent = {"sales": 0.0, "orders": 0, "refunded": 0.0}
    baseline_sales = 0.0
    for o in orders:
        # processedAt = when the sale happened (same date Shopify's own reports use).
        # Imported or backdated orders have an older processedAt than createdAt.
        created = datetime.fromisoformat(o["processedAt"].replace("Z", "+00:00"))
        amount = float(o["totalPriceSet"]["shopMoney"]["amount"])
        currency = o["totalPriceSet"]["shopMoney"]["currencyCode"]
        refunded = float(o["totalRefundedSet"]["shopMoney"]["amount"])
        if created >= last_24h_start:
            recent["sales"] += amount
            recent["orders"] += 1
            recent["refunded"] += refunded
        elif created >= baseline_start:
            baseline_sales += amount
    daily_avg = baseline_sales / 7
    refund_rate = recent["refunded"] / recent["sales"] if recent["sales"] else 0.0

    alerts = []
    if daily_avg == 0:
        status = "No baseline yet (no orders in the 7 days before). Comparison starts once there is history."
    elif recent["sales"] < daily_avg * DROP_THRESHOLD:
        status = "SALES DROP"
        alerts.append(f":red_circle: Sales are {recent['sales'] / daily_avg:.0%} of a normal day.")
    elif recent["sales"] > daily_avg * SPIKE_THRESHOLD:
        status = "SALES SPIKE"
        alerts.append(f":large_green_circle: Sales are {recent['sales'] / daily_avg:.0%} of a normal day. Check stock and ads.")
    else:
        status = "Normal"
    if refund_rate > REFUND_RATE_ALERT:
        alerts.append(f":warning: Refund rate is {refund_rate:.0%} (alert above {REFUND_RATE_ALERT:.0%}).")

    return {
        "currency": currency,
        "recent": recent,
        "daily_avg": daily_avg,
        "refund_rate": refund_rate,
        "status": status,
        "alerts": alerts,
    }


def build_message(store, r):
    c = r["currency"]
    lines = [
        f"*Daily sales check: {store}*",
        f"Status: *{r['status']}*",
        f"Last 24h: {r['recent']['orders']} orders, {r['recent']['sales']:,.2f} {c}",
        f"7-day daily average: {r['daily_avg']:,.2f} {c}",
        f"Refunded (last 24h): {r['recent']['refunded']:,.2f} {c} ({r['refund_rate']:.0%})",
    ]
    if r["alerts"]:
        lines.append("")
        lines.extend(r["alerts"])
    return "\n".join(lines)


def main():
    cfg = load_env()
    now = datetime.now(timezone.utc)
    try:
        token = get_access_token(cfg)
        orders = fetch_orders(cfg, token, now - timedelta(days=8))
        report = analyze(orders, now)
        message = build_message(cfg["store"], report)
    except Exception as e:
        message = f":rotating_light: *Sales check failed* for {cfg['store']}\n{e}"
        print(message)
        post_json(cfg["slack"], {"text": message})
        sys.exit(1)

    print(message)
    post_json(cfg["slack"], {"text": message})
    print("\nPosted to Slack.")


if __name__ == "__main__":
    main()
