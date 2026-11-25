"""Run the pinned accrual exporter with narrowly checked Ozon and Google access."""
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import gspread
import requests
from ozon_to_google_sheets.google_sheets import GoogleSheetsAdapter, ReliableHTTPClient
from ozon_to_google_sheets.models import TRANSACTION_SHEET_HEADER, parse_accrual_types
from ozon_to_google_sheets.ozon import OzonClient
from ozon_to_google_sheets.service import SyncService

BASE = "https://api-seller.ozon.ru"
ACCRUAL = "/v1/finance/accrual/"
REQUIRED = {ACCRUAL + name for name in ("by-day", "postings", "types")}
METADATA = "/v1/roles"
TAB = "KND_Accruals"
# Enumerated informational methods. An unfamiliar permission must be reviewed
# rather than treated as safe merely because it has "finance" in its name.
READ_METHODS = {
    "AccessAPI_RolesByToken": METADATA,
    "GetFinanceAccrualByDay": ACCRUAL + "by-day",
    "GetFinanceAccrualPostings": ACCRUAL + "postings",
    "GetFinanceAccrualTypes": ACCRUAL + "types",
    "GetFinanceBalanceV1": "/v1/finance/balance",
    "FinanceAPI_FinanceCashFlowStatementList": "/v1/finance/cash-flow-statement/list",
    "FinanceAPI_GetRealizationReportV2": "/v2/finance/realization",
    "FinanceAPI_GetRealizationReportV1": "/v1/finance/realization/posting",
    "FinanceAPI_GetRealizationByDayReportV1": "/v1/finance/realization/by-day",
    "FinanceAPI_FinanceTransactionListV3": "/v3/finance/transaction/list",
    "FinanceAPI_FinanceTransactionTotalV3": "/v3/finance/transaction/totals",
    "GetFinanceProductsBuyout": "/v1/finance/products/buyout",
    "ReportAPI_GetCompensationReport": "/v1/finance/compensation",
    "ReportAPI_GetDecompensationReport": "/v1/finance/decompensation",
    "invoice_getV2": "/v2/invoice/get",
    "GetReceipt": "/v1/receipts/get",
    "ReceiptsSellerList": "/v1/receipts/seller/list",
}
ALLOWED = set(READ_METHODS.values()) | {
    "/v1/finance/mutual-settlement", "/v1/finance/document-b2b-sales",
    "/v1/report/finance/realization/list",
}
OZON_TARGETS = {BASE + path for path in REQUIRED | {METADATA}}


class Stop(Exception):
    """Only locally defined non-sensitive messages are suitable for public CI."""


def canonical_method(value):
    if isinstance(value, dict):
        path = value.get("path") or value.get("url")
        if path:
            if value.get("method", "POST") not in ("GET", "POST"):
                raise Stop("Unverified Ozon permission or method.")
            value = path
        else:
            value = value.get("name") or value.get("method")
    if not isinstance(value, str):
        raise Stop("Unsupported Ozon permissions format.")
    name = value.strip()
    if name.startswith(("POST ", "GET ")):
        name = name.split(None, 1)[1]
    return READ_METHODS.get(name, name)


def check_permissions(payload):
    if not isinstance(payload, dict):
        raise Stop("Unsupported Ozon permissions format.")
    info = payload.get("result", payload)
    roles = info.get("roles") if isinstance(info, dict) else None
    if not isinstance(roles, list) or not roles:
        raise Stop("Ozon did not return verifiable permissions.")
    granted = set()
    for role in roles:
        if not isinstance(role, dict) or not role.get("name"):
            raise Stop("Unsupported Ozon role format.")
        if "admin" in str(role["name"]).casefold():
            raise Stop("Broad Ozon admin keys are refused.")
        methods = role.get("methods")
        if not isinstance(methods, list) or not methods:
            raise Stop("Incomplete Ozon permissions list.")
        granted.update(canonical_method(m) for m in methods)
    if granted - ALLOWED:
        raise Stop("The Ozon key grants other or unverified methods.")
    if REQUIRED - granted:
        raise Stop("The Ozon key lacks required accrual methods.")


def ozon_post(url, **kwargs):
    if url not in OZON_TARGETS:
        raise Stop("Unexpected Ozon endpoint.")
    return requests.post(url, allow_redirects=False, **kwargs)


def run():
    mode = os.environ.get("FINANCE_MODE", "check")
    if mode not in ("check", "sync"):
        raise Stop("Invalid mode.")
    if not all(os.environ.get(k) for k in ("OZON_CLIENT_ID", "OZON_FINANCE_READONLY_KEY")):
        raise Stop("Set OZON_CLIENT_ID and OZON_FINANCE_READONLY_KEY in Actions secrets.")
    client = OzonClient(os.environ["OZON_FINANCE_READONLY_KEY"], os.environ["OZON_CLIENT_ID"], post=ozon_post)
    try:
        response = ozon_post(BASE + METADATA, headers=client.headers, json={}, timeout=(5, 30))
        if response.status_code != 200:
            raise Stop(f"Ozon role check returned HTTP {response.status_code}.")
        check_permissions(response.json())
        response = ozon_post(BASE + ACCRUAL + "types", headers=client.headers, json={}, timeout=(5, 30))
        if response.status_code != 200:
            raise Stop(f"Ozon accrual check returned HTTP {response.status_code}.")
        parse_accrual_types(response.json())
    except Stop:
        raise
    except Exception:
        raise Stop("Ozon role or accrual response could not be verified.") from None
    if not all(os.environ.get(k) for k in ("GOOGLE_SERVICE_ACCOUNT_JSON", "GOOGLE_SPREADSHEET_ID")):
        raise Stop("Ozon read verified; set GOOGLE_SERVICE_ACCOUNT_JSON and GOOGLE_SPREADSHEET_ID.")
    try:
        scope = "https://www.googleapis.com/auth/spreadsheets"
        credentials = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
        if credentials.get("type") != "service_account" or credentials.get("token_uri") != "https://oauth2.googleapis.com/token":
            raise Stop("Unsupported Google service account credentials.")
        google = gspread.service_account_from_dict(credentials,
            scopes=[scope + ".readonly" if mode == "check" else scope],
            http_client=ReliableHTTPClient)
        worksheet = google.open_by_key(os.environ["GOOGLE_SPREADSHEET_ID"]).worksheet(TAB)
        header = worksheet.get("A1:W1")
        if header and list(header[0]) != list(TRANSACTION_SHEET_HEADER):
            raise Stop("KND_Accruals must be empty or have the exporter's existing header.")
    except Stop:
        raise
    except Exception:
        raise Stop("Could not read the dedicated Google sheet or KND_Accruals tab.") from None
    if mode == "check":
        print("Verified Ozon's restricted read access and Google tab read access.")
        return
    today = datetime.now(ZoneInfo("Europe/Moscow")).date()
    SyncService(client, GoogleSheetsAdapter(worksheet), BASE + ACCRUAL + "by-day",
                today - timedelta(days=1), today).run()
    print("Accruals synchronized to the dedicated Google tab.")


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    try:
        run()
    except Stop as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    except Exception:
        print("Synchronization failed. No account data is shown in CI.", file=sys.stderr)
        sys.exit(1)
