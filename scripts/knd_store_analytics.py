"""Download account analytics. Only the endpoints enumerated below are permitted."""
import base64
import gzip
import http.client
import json
import os
import subprocess
import time
from datetime import datetime, time as day_time, timedelta, timezone
from zoneinfo import ZoneInfo

ALLOWED = {
    "/v3/product/list", "/v3/product/info/list", "/v5/product/info/prices",
    "/v2/product/info/stocks-by-warehouse/fbs", "/v4/posting/fbs/list", "/v1/returns/list",
    "/v1/search-queries/text", "/v1/analytics/product-queries", "/v1/analytics/data", "/v1/finance/accrual/by-day", "/v1/finance/accrual/types",
    "/v1/finance/accrual/postings", "/v1/finance/cash-flow-statement/list", "/v1/finance/balance",
}
MOSCOW = ZoneInfo("Europe/Moscow")
now = datetime.now(MOSCOW)
today = now.date()
periods = {"current": [today-timedelta(days=27), today], "previous": [today-timedelta(days=55),today-timedelta(days=28)]}
failed = {}
out = {"asof":now.isoformat(), "periods":periods,"failures":failed,"orders":{},"returns":{},"funnel":{},"cash":{},"finance":{"by_day":{}}}
headers = {"Client-Id":os.environ["OZON_CLIENT_ID"].strip(), "Api-Key":os.environ["OZON_API_KEY"].strip(), "Content-Type":"application/json"}
last_analytics_request = None
class ApiError(Exception):
    pass
def post(path, data):
    global last_analytics_request
    if path not in ALLOWED:
        raise ValueError("Unknown endpoint")
    for attempt in range(3):
        if path == "/v1/analytics/data":
            if last_analytics_request is not None:
                time.sleep(max(0, 60.1-(time.monotonic()-last_analytics_request)))
            last_analytics_request = time.monotonic()
        conn=http.client.HTTPSConnection("api-seller.ozon.ru",timeout=30)
        try:
            conn.request("POST",path,json.dumps(data),headers)
            res=conn.getresponse()
            raw=res.read()
            if res.status==200:
                return json.loads(raw)
            if res.status not in (429,500,502,503,504) or attempt==2:
                try:
                    msg=str(json.loads(raw).get("message",""))[:220]
                except ValueError:msg=""
                raise ApiError(res.status,msg)
            delay = max(1.5*(1+attempt), float(res.getheader("Retry-After") or 0))
        except (OSError, http.client.HTTPException):
            if attempt == 2:
                raise ApiError("connection", "Connection did not complete") from None
            delay = 1.5*(1+attempt)
        finally:conn.close()
        time.sleep(delay)
def attempt(label,fn):
    try:return fn()
    except ApiError as e:failed[label]={"status":e.args[0],"message":e.args[1]}
    except Exception as e:failed[label]={"status":"invalid_or_incomplete","message":type(e).__name__}
    return None
def pick(item, names):
    return {k:item[k] for k in names.split() if k in item}
def pages(path,body,array,token="cursor",root=None,flag="has_next"):
    rows=[];seen=set()
    while True:
        data=post(path,body)
        if root:data=data[root]
        page=data[array];rows.extend(page)
        follow=data.get(token)
        if flag in data and not data[flag]:break
        if not follow or not page:
            if data.get(flag):
                raise ValueError("Page continuation is missing")
            break
        if follow in seen:raise ValueError("Repeated page token")
        seen.add(follow);body[token]=follow
    return rows
def catalog():
    data=pages("/v3/product/list",{"filter":{"visibility":"ALL"},"limit":1000,"last_id":""},"items","last_id","result")
    ids=[r["product_id"] for r in data]
    result=[]
    for i in range(0,len(ids),1000):
        result.extend(pick(r,"id sku name offer_id statuses created_at updated_at is_archived is_autoarchived is_discounted price min_price old_price currency_code visibility_details stocks promotions primary_image") for r in post("/v3/product/info/list",{"product_id":ids[i:i+1000]})["items"])
    return result
def stock():
    if out["catalog"] is None:raise ValueError("Catalog unavailable")
    ids=[x["offer_id"] for x in out["catalog"]];result=[]
    for i in range(0,len(ids),100):
        result+=pages("/v2/product/info/stocks-by-warehouse/fbs",{"offer_id":ids[i:i+100],"limit":1000},"products")
    return result
def utc(dt):return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00","Z")
def cutoff(first,last):
    start=first if isinstance(first,datetime) else datetime.combine(first,day_time.min,MOSCOW)
    end=min(datetime.combine(last,day_time.max,MOSCOW),now)
    if config_line is None and last == periods["previous"][1]:
        end = now - timedelta(days=28)
    return utc(start),utc(end)
def orders(first,last):
    a,b=cutoff(first,last)
    rows=pages("/v4/posting/fbs/list",{"filter":{"since":a,"to":b},"limit":100,"sort_dir":"ASC","with":{"analytics_data":False,"financial_data":True,"legal_info":False,"barcodes":False}},"postings")
    result=[]
    for x in rows:
        record=pick(x,"posting_number order_id status substatus in_process_at shipment_date delivering_date")
        record["products"]=[pick(r,"offer_id name sku price quantity currency_code") for r in x.get("products",[])]
        record["financial_products"]=[pick(r,"product_id commission_amount commission_percent payout price old_price total_discount_value total_discount_percent quantity currency_code customer_price customer_currency_code") for r in (x.get("financial_data") or {}).get("products",[])]
        result.append(record)
    return result
def returns(first,last):
    a,b=cutoff(first,last)
    f={"logistic_return_date":{"time_from":a,"time_to":b}}
    result=[];cursor=0
    while True:
        data=post("/v1/returns/list",{"filter":f,"limit":500,"last_id":cursor})
        for x in data["returns"]:
            r=pick(x,"id order_id posting_number type schema return_reason_name visual product");r["logistic"]=pick(x.get("logistic") or {},"return_date final_moment technical_return_moment");result.append(r)
        if not data.get("has_next"):return result
        if data["returns"][-1]["id"]==cursor:raise ValueError("Repeated cursor")
        cursor=data["returns"][-1]["id"]
def analytics(first,last,metrics):
    rows=[];offset=0
    while True:
        resp=post("/v1/analytics/data",{"date_from":str(first),"date_to":str(last),"dimension":["sku"],"metrics":metrics,"limit":1000,"offset":offset})["result"]
        batch=resp["data"]
        if any(len(row.get("metrics", [])) != len(metrics) for row in batch) or len(resp.get("totals", [])) != len(metrics):
            raise ApiError("incomplete_metrics", "Response omitted requested metric columns")
        rows+=batch
        if len(batch)<1000:return {"metrics":metrics,"rows":rows,"totals":resp.get("totals",[])}
        offset+=1000
def cash(first,last):
    a,b=cutoff(first,last)
    pages_out=[];page=1
    while True:
        r=post("/v1/finance/cash-flow-statement/list",{"date":{"from":a,"to":b},"page":page,"page_size":100,"with_details":True})["result"]
        pages_out.append(r)
        if page>=int(r.get("page_count") or 1):return pages_out
        page+=1
def balance(first,last):
    return post("/v1/finance/balance",{"date_from":str(first),"date_to":str(last)})
def daily(day):
    return pages("/v1/finance/accrual/by-day",{"date":day,"last_id":""},"accruals","last_id")

def incremental(config):
    def stamp(key):
        value=datetime.fromisoformat(config[key])
        if value.tzinfo is None or value > now:
            raise ValueError("Invalid synchronization boundary")
        return value
    def day(key):
        value=datetime.fromisoformat(config[key]).date()
        if value > today or (today-value).days > 90:
            raise ValueError("Invalid settlement boundary")
        return value
    order_from=stamp("orders")
    return_from=stamp("returns")
    finance_from=day("finance")
    cash_from=day("cash")
    out["mode"]="incremental"
    out.pop("periods",None)
    out.pop("funnel",None)
    out["windows"]={"orders": order_from.isoformat(), "returns": return_from.isoformat(),
                    "finance":str(finance_from), "cash":str(cash_from)}
    open_days=sorted({datetime.fromisoformat(x).date() for x in config.get("open_days", [])})
    order_date=order_from.astimezone(MOSCOW).date()
    first_query=order_date if order_date in open_days else order_from
    out["orders"]["current"]=attempt("orders.current",lambda:orders(first_query,today))
    out["orders"]["open_days"]={}
    for d in open_days:
        if d>=order_date:
            continue
        result=attempt("orders.open."+str(d),lambda:orders(d,d))
        out["orders"]["open_days"][str(d)]=result
    out["returns"]["current"]=attempt("returns.current",lambda:returns(return_from,today))
    out["cash"]["current"]=attempt("cash.current",lambda:cash(cash_from,today))
    out["balance"]={"current":attempt("balance.current",lambda:balance(finance_from,today))}
    by_day=out["finance"]["by_day"]
    for offset in range((today-finance_from).days,-1,-1):
        target=str(today-timedelta(days=offset))
        by_day[target]=attempt("finance."+target,lambda:daily(target))
    parents=[x for rows in by_day.values() if isinstance(rows,list) for x in rows]
    if parents:
        out["finance"]["types"]=attempt("finance.types",lambda:post("/v1/finance/accrual/types",{}))
    # By-day records already contain signed total and line items. Detail is queried
    # separately only by the weekly reconcile if the components cannot explain it.

config_line=next((line[9:] for line in open("analytics/recipient.pem",encoding="ascii")
                  if line.startswith("KND_SYNC ")),None)
out["catalog"]=attempt("catalog",catalog)
out["prices"]=attempt("prices",lambda:pages("/v5/product/info/prices",{"filter":{"visibility":"ALL"},"limit":1000},"items"))
out["stocks"]=attempt("stocks",stock)
if config_line is not None:
    incremental(json.loads(config_line))
else:
    out["balance"]={}
    out["funnel_checks"]={}
    for label,(first,last) in periods.items():
        out["orders"][label]=attempt("orders."+label,lambda:orders(first,last))
        out["returns"][label]=attempt("returns."+label,lambda:returns(first,last))
        out["cash"][label]=attempt("cash."+label,lambda:cash(first,last))
        out["balance"][label]=attempt("balance."+label,lambda:balance(first,last))
        combined=["hits_view_search","hits_view_pdp","hits_tocart_pdp","hits_tocart","ordered_units","delivered_units","returns","cancellations","revenue"]
        funnel=attempt("funnel."+label,lambda:analytics(first,last,combined))
        if funnel is None:
            funnel=attempt("funnel.basic."+label,lambda:analytics(first,last,["ordered_units","revenue"]))
        out["funnel"][label]=funnel
    out["finance"]["types"]=attempt("finance.types",lambda:post("/v1/finance/accrual/types",{}))
    by_day=out["finance"]["by_day"]
    for offset in range(55,-1,-1):
        day=(today-timedelta(days=offset)).isoformat()
        by_day[day]=attempt("finance."+day,lambda:daily(day))
    postings={o.get("posting_number") for rows in out["orders"].values() for o in rows or []}
    postings.update(o.get("unit_number") for rows in by_day.values() for o in rows or [] if o.get("accrued_category")=="POSTING")
    numbers=sorted(x for x in postings if x)
    def detail():
        return [post("/v1/finance/accrual/postings",{"posting_numbers":numbers[i:i+100]}) for i in range(0,len(numbers),100)]
    out["finance"]["details"]=attempt("finance.details",detail)
    out["search_queries"]={}
    for text in ("маркировка кабеля","держатель для крышек","брелок аксолотль"):
        key="search."+text
        out["search_queries"][text]=attempt(key,lambda:post("/v1/search-queries/text",{"limit":5,"offset":0,"text":text}))
        if failed.get(key,{}).get("status") in (401,403):break
    if out["catalog"]:
        a,b=cutoff(periods["current"][0],today-timedelta(days=1))
        skus=[str(x["sku"]) for x in out["catalog"] if x.get("sku")]
        out["product_queries"]=attempt("product_queries",lambda:post("/v1/analytics/product-queries",{"date_from":a,"date_to":b,"page_size":1000,"page":0,"skus":skus}))
blob=gzip.compress(json.dumps(out,ensure_ascii=False,default=str).encode())
proc=subprocess.run(["openssl","cms","-encrypt","-aes-256-cbc","-binary","-outform","DER","analytics/recipient.pem"],input=blob,capture_output=True,check=True)
encoded=base64.b64encode(proc.stdout).decode()
for i in range(0,len(encoded),2000):
    print("KND_ENC:"+encoded[i:i+2000])
