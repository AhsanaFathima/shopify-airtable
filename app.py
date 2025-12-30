import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

print("🚀 Flask app starting...", flush=True)

# ---------- ENV ----------
SHOP = os.environ["SHOPIFY_SHOP"]
TOKEN = os.environ["SHOPIFY_API_TOKEN"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2024-07")
PREFERRED_LOCATION_ID = os.getenv("SHOPIFY_LOCATION_ID")

# ---------- MARKET MAPPING ----------
MARKET_NAMES = {
    "UAE": "United Arab Emirates",
    "Asia": "Asia Market with 55 rate",
    "America": "America catlog",
}

# ---------- CACHE ----------
CACHED_PRICE_LISTS = None
CACHED_PRIMARY_LOCATION_ID = None

# ---------- HELPERS ----------
def _json_headers():
    return {
        "X-Shopify-Access-Token": TOKEN,
        "Content-Type": "application/json",
    }

def _graphql_url():
    return f"https://{SHOP}/admin/api/{API_VERSION}/graphql.json"

def _rest_url(path):
    return f"https://{SHOP}/admin/api/{API_VERSION}/{path}"

def _to_number(x):
    try:
        return float(x) if x not in (None, "") else None
    except Exception:
        return None

# ---------- GRAPHQL ----------
def shopify_graphql(query, variables=None):
    resp = requests.post(
        _graphql_url(),
        headers=_json_headers(),
        json={"query": query, "variables": variables},
    )
    resp.raise_for_status()
    return resp.json()

# ---------- PRICE LISTS ----------
def get_market_price_lists():
    global CACHED_PRICE_LISTS

    if CACHED_PRICE_LISTS:
        return CACHED_PRICE_LISTS

    QUERY = """
    query {
      catalogs(first: 20, type: MARKET) {
        nodes {
          title
          status
          priceList { id currency }
        }
      }
    }
    """

    res = shopify_graphql(QUERY)
    price_lists = {}

    for c in res.get("data", {}).get("catalogs", {}).get("nodes", []):
        if c.get("status") == "ACTIVE" and c.get("priceList"):
            price_lists[c["title"]] = {
                "id": c["priceList"]["id"],
                "currency": c["priceList"]["currency"],
            }

    print("📊 Price lists:", price_lists, flush=True)
    CACHED_PRICE_LISTS = price_lists
    return price_lists

# ---------- VARIANT ----------
def get_variant_product_and_inventory_by_sku(sku):
    QUERY = """
    query ($q: String!) {
      productVariants(first: 1, query: $q) {
        nodes {
          id
          product { id }
        }
      }
    }
    """

    res = shopify_graphql(QUERY, {"q": f"sku:{sku}"})
    nodes = res.get("data", {}).get("productVariants", {}).get("nodes", [])

    if not nodes:
        return None, None, None, None

    variant_gid = nodes[0]["id"]
    product_gid = nodes[0]["product"]["id"]
    variant_id = variant_gid.split("/")[-1]

    r = requests.get(_rest_url(f"variants/{variant_id}.json"), headers=_json_headers())
    r.raise_for_status()
    inventory_item_id = r.json()["variant"]["inventory_item_id"]

    return variant_gid, product_gid, variant_id, inventory_item_id

# ---------- CURRENT PRICES ----------
def get_variant_default_price(variant_id):
    r = requests.get(_rest_url(f"variants/{variant_id}.json"), headers=_json_headers())
    r.raise_for_status()
    return float(r.json()["variant"]["price"])

# ---------- UPDATE PRICES (UNCHANGED LOGIC) ----------
def update_variant_default_price(variant_id, price, compare_price=None):
    payload = {"variant": {"id": int(variant_id), "price": str(price)}}
    if compare_price is not None:
        payload["variant"]["compare_at_price"] = str(compare_price)

    print("💲 Updating UAE price →", payload, flush=True)
    requests.put(
        _rest_url(f"variants/{variant_id}.json"),
        headers=_json_headers(),
        json=payload,
    ).raise_for_status()

def update_price_list(price_list_id, variant_gid, price, currency, compare_price=None):
    print(
        f"➡️ Updating price list {price_list_id} → price={price}, compare={compare_price}",
        flush=True
    )

    price_input = {
        "variantId": variant_gid,
        "price": {"amount": str(price), "currencyCode": currency},
    }

    if compare_price is not None:
        price_input["compareAtPrice"] = {
            "amount": str(compare_price),
            "currencyCode": currency,
        }

    MUTATION = """
    mutation ($pl: ID!, $prices: [PriceListPriceInput!]!) {
      priceListFixedPricesAdd(priceListId: $pl, prices: $prices) {
        userErrors { message }
      }
    }
    """

    shopify_graphql(MUTATION, {"pl": price_list_id, "prices": [price_input]})

# ---------- VARIANT / PRODUCT DETAILS ----------
def update_variant_details(variant_gid, title=None, barcode=None):
    if not (title or barcode):
        return

    var_id = variant_gid.split("/")[-1]
    payload = {"variant": {"id": int(var_id)}}

    if title and str(title).strip():
        payload["variant"]["title"] = title

    if barcode and str(barcode).strip():
        payload["variant"]["barcode"] = barcode
        print(f"🧾 Updating barcode → {barcode}", flush=True)

    r = requests.put(
        _rest_url(f"variants/{var_id}.json"),
        headers=_json_headers(),
        json=payload,
    )
    r.raise_for_status()

def update_product_title(product_gid, title):
    if not title:
        return

    pid = product_gid.split("/")[-1]
    r = requests.put(
        _rest_url(f"products/{pid}.json"),
        headers=_json_headers(),
        json={"product": {"id": int(pid), "title": title}},
    )
    r.raise_for_status()

# ---------- METAFIELDS ----------
def set_metafield(owner_id_gid, namespace, key, mtype, value):
    MUT = """
    mutation metafieldsSet($metafields: [MetafieldsSetInput!]!) {
      metafieldsSet(metafields: $metafields) {
        userErrors { message }
      }
    }
    """
    shopify_graphql(MUT, {
        "metafields": [{
            "ownerId": owner_id_gid,
            "namespace": namespace,
            "key": key,
            "type": mtype,
            "value": str(value)
        }]
    })

# ---------- INVENTORY ----------
def get_primary_location_id():
    global CACHED_PRIMARY_LOCATION_ID
    if PREFERRED_LOCATION_ID:
        return PREFERRED_LOCATION_ID
    if CACHED_PRIMARY_LOCATION_ID:
        return CACHED_PRIMARY_LOCATION_ID

    r = requests.get(_rest_url("locations.json"), headers=_json_headers())
    r.raise_for_status()
    loc = r.json()["locations"][0]["id"]
    CACHED_PRIMARY_LOCATION_ID = str(loc)
    return CACHED_PRIMARY_LOCATION_ID

def set_inventory_absolute(item_id, location_id, qty):
    requests.post(
        _rest_url("inventory_levels/set.json"),
        headers=_json_headers(),
        json={
            "inventory_item_id": int(item_id),
            "location_id": int(location_id),
            "available": int(qty),
        },
    ).raise_for_status()

# ---------- ROUTES ----------
@app.route("/airtable-webhook", methods=["POST"])
def airtable_webhook():
    if (request.headers.get("X-Secret-Token") or "").strip() != WEBHOOK_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.json or {}
    sku = data.get("SKU")

    if not sku:
        return jsonify({"error": "SKU missing"}), 400

    prices = {
        "UAE": _to_number(data.get("UAE price")),
        "Asia": _to_number(data.get("Asia Price")),
        "America": _to_number(data.get("America Price")),
    }

    compare = {
        "UAE": _to_number(data.get("UAE Comparison Price")),
        "Asia": _to_number(data.get("Asia Comparison Price")),
        "America": _to_number(data.get("America Comparison Price")),
    }

    title = data.get("Title")
    barcode = data.get("Barcode")
    size = data.get("Size")
    qty = _to_number(data.get("Qty given in shopify"))

    variant_gid, product_gid, variant_id, item_id = get_variant_product_and_inventory_by_sku(sku)
    if not variant_gid:
        return jsonify({"error": "Variant not found"}), 404

    update_variant_details(variant_gid, title, barcode)
    update_product_title(product_gid, title)

    if size:
        set_metafield(variant_gid, "custom", "size", "single_line_text_field", size)

    if prices["UAE"] is not None:
        update_variant_default_price(variant_id, prices["UAE"], compare["UAE"])

    if qty is not None:
        loc = get_primary_location_id()
        set_inventory_absolute(item_id, loc, qty)

    price_lists = get_market_price_lists()
    for m, p in prices.items():
        if p is None:
            continue
        pl = price_lists.get(MARKET_NAMES.get(m))
        if pl:
            update_price_list(pl["id"], variant_gid, p, pl["currency"], compare[m])

    return jsonify({"status": "success"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
