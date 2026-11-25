"""Submit an authorized text-only update for the seven approved KND listings."""

import base64
import gzip
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import re
import sys
import time

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

EXPECTED = {
    "KND-DOM-DACHA-2": "6464958645",
    "KND-DOM-BLK-1": "6464999096",
    "KND-DACHA-RED-1": "6464999051",
    "KND-DOM-DACHA-BO-2": "6464999090",
    "KND-DOM-DACHA-MIX-4": "6464999300",
    "KND-CLIP-6-OLIVE": "6476210340",
    "0002": "5944250800",
}
CONTEXT = b"KND catalog description update 2026-11-25 5de249c46d3a"
PRODUCTS = "/v4/product/info/attributes"
CATEGORY = "/v1/description-category/attribute"
INFO = "/v3/product/info/list"
UPDATE = "/v1/product/attributes/update"
STATUS = "/v1/product/import/info"
READ_METHODS = {PRODUCTS, CATEGORY, INFO, STATUS}


def report(event, value):
    print(event + "=" + json.dumps(value, ensure_ascii=False, separators=(",", ":")), flush=True)


def credentials():
    return {name: os.environ[var].strip() for name, var in (
        ("Client-Id", "OZON_CLIENT_ID"), ("Api-Key", "OZON_API_KEY")
    )}


def post(path, params, allow_update=False):
    if path not in READ_METHODS and not (path == UPDATE and allow_update):
        raise ValueError("Method outside allowed catalog scope")
    headers = {**credentials(), "Content-Type": "application/json"}
    for attempt in range(3 if path in READ_METHODS else 1):
        connection = http.client.HTTPSConnection("api-seller.ozon.ru", timeout=18)
        try:
            connection.request("POST", path, json.dumps(params, ensure_ascii=False).encode(), headers)
            response = connection.getresponse()
            raw = response.read()
            if response.status == 200:
                return json.loads(raw)
            if response.status not in (429, 500, 502, 503, 504) or attempt == 2 or path == UPDATE:
                error = json.loads(raw) if raw.startswith(b"{") else {}
                report("OZON_ERROR", {"path": path, "status": response.status, "code": error.get("code"),
                                      "message": str(error.get("message", ""))[:180]})
                raise RuntimeError("Ozon returned an error")
        finally:
            connection.close()
        time.sleep(2 + 2 * attempt)


def receiver_key():
    seed = hmac.digest(credentials()["Api-Key"].encode(), CONTEXT, "sha256")
    return X25519PrivateKey.from_private_bytes(seed)


def shared_key(private, public):
    return hashlib.sha256(CONTEXT + private.exchange(public)).digest()


def seal(receiver, rows, mode):
    private = X25519PrivateKey.generate()
    public = X25519PublicKey.from_public_bytes(bytes.fromhex(receiver))
    nonce = os.urandom(12)
    data = gzip.compress(json.dumps(rows, ensure_ascii=False).encode())
    encrypted = AESGCM(shared_key(private, public)).encrypt(nonce, data, CONTEXT + mode.encode())
    return {"mode": mode, "sender": private.public_key().public_bytes_raw().hex(),
            "nonce": nonce.hex(), "ciphertext": base64.b64encode(encrypted).decode("ascii")}


def open_request(request):
    sender = X25519PublicKey.from_public_bytes(bytes.fromhex(request["sender"]))
    decoded = AESGCM(shared_key(receiver_key(), sender)).decrypt(
        bytes.fromhex(request["nonce"]), base64.b64decode(request["ciphertext"]), CONTEXT + request["mode"].encode()
    )
    items = json.loads(gzip.decompress(decoded))
    if len(items) != len(EXPECTED) or {row["offer_id"]: row["sku"] for row in items} != EXPECTED:
        raise ValueError("Approved product list differs from expected SKUs")
    for row in items:
        if not 20 <= len(row["description"]) <= 6000 or any(mark in row["description"] for mark in ("http", "#")):
            raise ValueError("Invalid description")
        tags = row["tags"]
        if not 1 <= len(tags) <= 30 or len(set(tags)) != len(tags):
            raise ValueError("Incorrect number of hashtags")
        if not all(re.fullmatch(r"#[а-яёa-z0-9_]{1,29}", tag, re.I) for tag in tags):
            raise ValueError("Invalid hashtag")
    return {row["offer_id"]: row for row in items}


def products():
    result = post(PRODUCTS, {"filter": {"offer_id": list(EXPECTED), "visibility": "ALL"}, "limit": 100})["result"]
    by_offer = {row["offer_id"]: row for row in result}
    if {offer: str(row.get("sku", "")) for offer, row in by_offer.items()} != EXPECTED:
        raise ValueError("Catalog SKUs differ from the approved scope")
    return by_offer


def catalog(products_by_offer):
    values = post(INFO, {"product_id": [row["id"] for row in products_by_offer.values()]})["items"]
    by_offer = {row["offer_id"]: row for row in values}
    if set(by_offer) != set(EXPECTED):
        raise ValueError("Ozon returned an incomplete catalog")
    return by_offer


def discovered_fields(products_by_offer):
    fields_by_category = {}
    for row in products_by_offer.values():
        category = (int(row["description_category_id"]), int(row["type_id"]))
        if category in fields_by_category:
            continue
        fields = post(CATEGORY, {"description_category_id": category[0], "type_id": category[1],
                                "language": "RU"})["result"]
        description = [f for f in fields if f["id"] == 4191 and f["name"].strip().lower() == "аннотация"]
        hashtags = [f for f in fields if f["name"].strip().lower() in {"хештеги", "хэштеги"}]
        if len(description) != 1 or len(hashtags) > 1:
            report("CATEGORY_FIELDS", {"category": category, "annotations": len(description), "hashtags": len(hashtags)})
            raise ValueError("Could not identify both catalog fields")
        if not hashtags:
            # This store already uses the universal hashtag field as a single
            # space-delimited string, even if it is absent from a category's schema.
            hashtags = [{"id": 23171, "is_collection": False, "max_value_count": 30}]
        for field in (description[0], hashtags[0]):
            if field.get("dictionary_id") or field.get("attribute_complex_id"):
                raise ValueError("Expected free-text, non-complex attributes")
        fields_by_category[category] = (description[0], hashtags[0])
    return {offer: fields_by_category[(int(row["description_category_id"]), int(row["type_id"]))]
            for offer, row in products_by_offer.items()}


def current_values(product, field):
    return [item["value"] for attr in product.get("attributes", []) if attr["id"] == field
            for item in attr.get("values", []) if item.get("value")]


def normalize(text):
    return " ".join(text.split())


def desired_fields(row, fields):
    description, hashtags = fields
    if hashtags.get("max_value_count") and len(row["tags"]) > int(hashtags["max_value_count"]):
        raise ValueError("Approved hashtags exceed a product category limit")
    tags = row["tags"] if hashtags.get("is_collection") else [" ".join(row["tags"])]
    return {description["id"]: [row["description"]], hashtags["id"]: tags}


def body_slots(node):
    if isinstance(node, dict):
        text = node.get("text")
        if isinstance(text, dict) and isinstance(text.get("content"), list):
            content = text["content"]
            if content and all(isinstance(line, str) for line in content):
                yield text
        for key, value in node.items():
            if key != "text":
                yield from body_slots(value)
    elif isinstance(node, list):
        for item in node:
            yield from body_slots(item)


def rich_with_approved_text(product, description):
    existing = current_values(product, 11254)
    if len(existing) != 1:
        return None
    model = json.loads(existing[0])
    if not isinstance(model, dict) or not isinstance(model.get("content"), list):
        return None
    slots = list(body_slots(model["content"]))
    if not slots:
        return None
    paragraphs = [p.strip() for p in description.split("\n\n") if p.strip()]
    count = min(len(slots), len(paragraphs))
    for slot, paragraph in zip(slots[:count - 1], paragraphs[:count - 1]):
        slot["content"] = [paragraph]
    slots[count - 1]["content"] = paragraphs[count - 1:]
    return json.dumps(model, ensure_ascii=False, separators=(",", ":"))


def matches(product, desired):
    description, hashtags = list(desired.items())[:2]
    annotation_ok = list(map(normalize, current_values(product, description[0]))) == list(map(normalize, description[1]))
    tokens = [tag.lower().lstrip("#") for value in current_values(product, hashtags[0]) for tag in value.split()]
    hashtag_ok = set(tokens) == {tag.lower().lstrip("#") for value in hashtags[1] for tag in value.split()}
    result = {"description": annotation_ok, "hashtags": hashtag_ok}
    if 11254 in desired:
        stored = current_values(product, 11254)
        result["visible_description"] = len(stored) == 1 and json.loads(stored[0]) == json.loads(desired[11254][0])
    return result


def summaries(rows):
    return {offer: {"visible": row.get("statuses", {}).get("status_name"),
                    "moderation": row.get("statuses", {}).get("moderate_status"),
                    "validation": row.get("statuses", {}).get("validation_status")}
            for offer, row in rows.items()}


def stable_catalog(before, after):
    keys = ("name", "primary_image", "images", "type_id", "description_category_id",
            "height", "width", "depth", "weight", "dimension_unit", "weight_unit")
    return all(all(row.get(k) == after[offer].get(k) for k in keys) for offer, row in before.items())


def verify(live, desired):
    return {offer: matches(product, desired[offer]) for offer, product in live.items()}


def build_desired(approved, live, category_fields):
    desired = {}
    for offer, row in approved.items():
        fields = desired_fields(row, category_fields[offer])
        rich = rich_with_approved_text(live[offer], row["description"])
        if rich is not None:
            fields[11254] = [rich]
        desired[offer] = fields
    return desired


def apply(approved, live, category_fields):
    desired = build_desired(approved, live, category_fields)
    before = verify(live, desired)
    items = []
    for offer, fields in desired.items():
        if all(before[offer].values()):
            continue
        updates = [{"id": field, "complex_id": 0,
                    "values": [{"dictionary_value_id": 0, "value": value} for value in values]}
                   for field, values in fields.items()
                   if not before[offer]["description" if field == 4191 else
                                        "visible_description" if field == 11254 else "hashtags"]]
        items.append({"offer_id": offer, "attributes": updates})
    report("MATCH_BEFORE", before)
    if not items:
        report("MATCH_AFTER", before)
        report("CATALOG_STATUS", summaries(catalog(live)))
        return
    task_id = int(post(UPDATE, {"items": items}, allow_update=True)["task_id"])
    report("OZON_TASK", task_id)
    final_task = {}
    for delay in (3, 5, 8, 10, 12, 15, 20, 25, 25, 30, 35):
        time.sleep(delay)
        task = post(STATUS, {"task_id": task_id})["result"]
        final_task = {row["offer_id"]: {"status": row.get("status"),
                      "errors": [{field: error[field] for field in ("code", "field", "attribute_id", "level")
                                  if field in error} for error in row.get("errors", [])]}
                      for row in task.get("items", [])}
        active = {"pending", "processing", "queued", "in_progress", ""}
        if len(final_task) == len(items) and all(str(row["status"] or "").lower() not in active
                                                 for row in final_task.values()):
            break
    report("OZON_IMPORT", final_task)
    for delay in (0, 5, 10, 20, 25):
        time.sleep(delay)
        updated = products()
        persisted = verify(updated, desired)
        if all(all(values.values()) for values in persisted.values()):
            break
    report("MATCH_AFTER", persisted)
    report("OTHER_DETAILS_MATCH", stable_catalog(live, updated))
    report("CATALOG_STATUS", summaries(catalog(updated)))


def main(request):
    live = products()
    fields = discovered_fields(live)
    state = catalog(live)
    if any(row.get("is_archived") or row.get("is_autoarchived") for row in state.values()):
        raise ValueError("An approved SKU has been archived")
    if request["mode"] == "inspect":
        report("RECEIVER_PUBLIC", receiver_key().public_key().public_bytes_raw().hex())
        report("AVAILABLE_FIELDS", {offer: [(f["id"], f.get("is_collection"), f.get("max_value_count"))
                                             for f in pair] for offer, pair in fields.items()})
        report("CATALOG_STATUS", summaries(state))
        report("EXISTING_CONTENT", {offer: {"tag_values": len(current_values(row, 23171)),
                                            "rich_content": bool(current_values(row, 11254))}
                                    for offer, row in live.items()})
    elif request["mode"] == "verify":
        approved = open_request(request)
        report("MATCH_AFTER", verify(live, build_desired(approved, live, fields)))
        report("CATALOG_STATUS", summaries(state))
    elif request["mode"] == "apply":
        apply(open_request(request), live, fields)
    else:
        raise ValueError("Unsupported operation")


if __name__ == "__main__":
    main(json.loads(Path(sys.argv[1]).read_text()))
