import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

print("🚀 Flask app starting...", flush=True)

# ---------- Env ----------
SHOP = os.environ["SHOPIFY_SHOP"]             # e.g., devfragrantsouq.myshopify.com
TOKEN = os.environ["SHOPIFY_API_TOKEN"]       # shpat_...
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
PREFERRED_LOCATION_ID = os.getenv("SHOPIFY_LOCATION_ID")
API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2024-07")

print("🔐 ENV CHECK", flush=True)
print("SHOPIFY_SHOP:", SHOP, flush=True)
print("API_VERSION:", API_VERSION, flush=True)
print("HAS TOKEN:", bool(TOKEN), flush=True)
print("HAS WEBHOOK_SECRET:", bool(WEBHOOK_SECRET), flush=True)
print("PREFERRED_LOCATION_ID:", PREFERRED_LOCATION_ID, flush=True)

# ---------- Market Mapping ----------
MARKET_NAMES = {
    "UAE": "United Arab Emirates",
    "Asia": "Asia Market",
    "America": "International Market",
}

# ---------- Cache ----------
CACHED_PRICE_LISTS = None
CACHED_PRIMARY_LOCATION_ID = None

# ---------- Helpers ----------
def _json_headers():
    return {
        "X-Shopify-Access-Token": TOKEN,
        "Content-Type": "application/json"
    }

def _graphql_url():
    return f"https://{SHOP}/admin/api/{API_VERSION}/graphql.json"

def _rest_url(path: str):
    return f"https://{SHOP}/admin/api/{API_VERSION}/{path}"

def _to_number(x):
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return x
    s = str(x).strip()
    if not s:
        return None
    try:
        return float(s) if "." in s else int(s)
    except Exception:
        return None

# ---------- GraphQL ----------
def shopify_graphql(query, variables=None):
    print("\n🧠 GRAPHQL CALL", flush=True)
    print("Query:", query, flush=True)
    print("Variables:", variables, flush=True)

    resp = requests.post(
        _graphql_url(),
        headers=_json_headers(),
        json={"query": query, "variables": variables}
    )

    print("GraphQL status:", resp.status_code, flush=True)
    print("GraphQL response:", resp.text, flush=True)
    resp.raise_for_status()
    return resp.json()

# ---------- Price Lists ----------
def get_market_price_lists():
    global CACHED_PRICE_LISTS

    if CACHED_PRICE_LISTS:
        print("♻️ Using cached price lists", flush=True)
        return CACHED_PRICE_LISTS

    print("\n📊 Fetching MARKET price lists (catalogs)...", flush=True)

    QUERY = """
    query {
      catalogs(first: 20, type: MARKET) {
        nodes {
          id
          title
          status
          priceList {
            id
            name
            currency
          }
        }
      }
    }
    """

    result = shopify_graphql(QUERY)
    price_lists = {}

    for c in result["data"]["catalogs"]["nodes"]:
        print(f"Catalog: {c['title']} | Status: {c['status']}", flush=True)
        if c["status"] != "ACTIVE":
            continue
        if c["priceList"]:
            price_lists[c["title"]] = {
                "id": c["priceList"]["id"],
                "currency": c["priceList"]["currency"]
            }

    print("✅ Final price list map:", price_lists, flush=True)
    CACHED_PRICE_LISTS = price_lists
    return price_lists

# ---------- Variant ----------
def get_variant_product_and_inventory_by_sku(sku):
    print(f"\n🔍 Searching Shopify variant by SKU: {sku}", flush=True)

    QUERY = """
    query ($sku: String!) {
      productVariants(first: 1, query: $sku) {
        nodes {
          id
          product { id }
        }
      }
    }
    """

    res = shopify_graphql(QUERY, {"sku": sku})
    nodes = res["data"]["productVariants"]["nodes"]

    if not nodes:
        print("❌ Variant not found", flush=True)
        return None, None, None, None

    variant_gid = nodes[0]["id"]
    product_gid = nodes[0]["product"]["id"]
    variant_id = variant_gid.split("/")[-1]

    r = requests.get(_rest_url(f"variants/{variant_id}.json"), headers=_json_headers())
    r.raise_for_status()
    inventory_item_id = r.json()["variant"]["inventory_item_id"]

    print("✅ Variant found", flush=True)
    print("Variant GID:", variant_gid, flush=True)
    print("Product GID:", product_gid, flush=True)
    print("Inventory Item ID:", inventory_item_id, flush=True)

    return variant_gid, product_gid, variant_id, inventory_item_id

# ---------- Default Price ----------
def update_variant_default_price(variant_id, price, compare_at=None):
    print(f"💲 Updating default price: {price}", flush=True)
    payload = {"variant": {"id": int(variant_id), "price": str(price)}}
    if compare_at:
        payload["variant"]["compare_at_price"] = str(compare_at)

    r = requests.put(_rest_url(f"variants/{variant_id}.json"),
                     headers=_json_headers(), json=payload)
    print("Default price response:", r.text, flush=True)
    r.raise_for_status()

# ---------- Inventory ----------
def set_inventory_absolute(item_id, location_id, qty):
    print(f"📦 Updating inventory → {qty}", flush=True)
    payload = {
        "inventory_item_id": int(item_id),
        "location_id": int(location_id),
        "available": int(qty)
    }
    r = requests.post(_rest_url("inventory_levels/set.json"),
                      headers=_json_headers(), json=payload)
    print("Inventory response:", r.text, flush=True)
    r.raise_for_status()

def get_primary_location_id():
    global CACHED_PRIMARY_LOCATION_ID
    if CACHED_PRIMARY_LOCATION_ID:
        return CACHED_PRIMARY_LOCATION_ID

    r = requests.get(_rest_url("locations.json"), headers=_json_headers())
    r.raise_for_status()
    loc = r.json()["locations"][0]["id"]
    CACHED_PRIMARY_LOCATION_ID = loc
    print("📍 Using location:", loc, flush=True)
    return loc

# ---------- Price List Update ----------
def update_price_list(price_list_id, variant_gid, price, currency):
    print(f"➡️ Updating price list {price_list_id}: {price} {currency}", flush=True)

    MUTATION = """
    mutation ($pl: ID!, $prices: [PriceListPriceInput!]!) {
      priceListFixedPricesAdd(priceListId: $pl, prices: $prices) {
        userErrors { message }
      }
    }
    """

    res = shopify_graphql(MUTATION, {
        "pl": price_list_id,
        "prices": [{
            "variantId": variant_gid,
            "price": {"amount": str(price), "currencyCode": currency}
        }]
    })

    print("Price list update response:", res, flush=True)

# ---------- Routes ----------
@app.route("/", methods=["GET"])
def home():
    return "Airtable-Shopify Sync Webhook is running!", 200

@app.route("/airtable-webhook", methods=["POST"])
def airtable_webhook():
    print("\n🔔 === AIRTABLE WEBHOOK HIT ===", flush=True)
    print("Headers:", dict(request.headers), flush=True)

    secret = request.headers.get("X-Secret-Token")
    print("🔑 Secret received:", secret, flush=True)
    print("🔑 Secret expected:", WEBHOOK_SECRET, flush=True)

    if secret != WEBHOOK_SECRET:
        print("❌ Unauthorized", flush=True)
        return jsonify({"error": "Unauthorized"}), 401

    data = request.json or {}
    print("📦 Payload:", data, flush=True)

    sku = data.get("SKU")
    prices = {
        "UAE": _to_number(data.get("UAE price")),
        "Asia": _to_number(data.get("Asia Price")),
        "America": _to_number(data.get("America Price"))
    }
    qty = _to_number(data.get("Qty given in shopify"))

    print("🆔 SKU:", sku, flush=True)
    print("💰 Prices:", prices, flush=True)

    variant_gid, product_gid, variant_id, inventory_item_id = get_variant_product_and_inventory_by_sku(sku)
    if not variant_gid:
        return jsonify({"error": "Variant not found"}), 404

    if prices["UAE"] is not None:
        update_variant_default_price(variant_id, prices["UAE"])

    if qty is not None:
        loc = get_primary_location_id()
        set_inventory_absolute(inventory_item_id, loc, qty)

    price_lists = get_market_price_lists()

    for market, price in prices.items():
        if price is None:
            continue
        market_name = MARKET_NAMES.get(market)
        if market_name not in price_lists:
            print(f"⚠️ No price list for {market}", flush=True)
            continue

        pl = price_lists[market_name]
        update_price_list(pl["id"], variant_gid, price, pl["currency"])

    print("🎉 === WEBHOOK COMPLETED SUCCESSFULLY ===", flush=True)
    return jsonify({"status": "success"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
