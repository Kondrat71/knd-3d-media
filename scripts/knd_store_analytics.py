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
class ApiError(Exception):
    pass
def post(path, data):
    if path not in ALLOWED:
        raise ValueError("Unknown endpoint")
    for attempt in range(4):
        conn=http.client.HTTPSConnection("api-seller.ozon.ru",timeout=30)
        try:
            conn.request("POST",path,json.dumps(data),headers)
            res=conn.getresponse()
            raw=res.read()
            if res.status==200:
                return json.loads(raw)
            if res.status not in (429,500,502,503,504) or attempt==3:
                try:
                    msg=str(json.loads(raw).get("message",""))[:220]
                except ValueError:msg=""
                raise ApiError(res.status,msg)
        finally:conn.close()
        time.sleep(1.5*(1+attempt))
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
        if not follow or not page:break
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
out["catalog"]=attempt("catalog",catalog)
out["prices"]=attempt("prices",lambda:pages("/v5/product/info/prices",{"filter":{"visibility":"ALL"},"limit":1000},"items"))
def stock():
    if out["catalog"] is None:raise ValueError("Catalog unavailable")
    ids=[x["offer_id"] for x in out["catalog"]];result=[]
    for i in range(0,len(ids),100):
        result+=pages("/v2/product/info/stocks-by-warehouse/fbs",{"offer_id":ids[i:i+100],"limit":1000},"products")
    return result
out["stocks"]=attempt("stocks",stock)
def utc(dt):return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00","Z")
def cutoff(first,last):
    start=datetime.combine(first,day_time.min,MOSCOW)
    end=min(datetime.combine(last,day_time.max,MOSCOW),now)
    if last == periods["previous"][1]:
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
        # Query each unavailable column separately in case this account allows a subset.
        checks={}
        for metric in ("hits_view_search","hits_view_pdp","hits_tocart_pdp","delivered_units","returns","cancellations"):
            checks[metric]=attempt("metric."+label+"."+metric,lambda:analytics(first,last,[metric]))
        out["funnel_checks"][label]=checks
    out["funnel"][label]=funnel
out["finance"]["types"]=attempt("finance.types",lambda:post("/v1/finance/accrual/types",{}))
by_day=out["finance"]["by_day"]
def daily(day):
    return pages("/v1/finance/accrual/by-day",{"date":day,"last_id":""},"accruals","last_id")
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
