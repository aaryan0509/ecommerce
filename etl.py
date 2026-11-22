"""ETL pipeline: raw CSVs -> cleaned, reconciled SQLite warehouse.

Run:  python etl.py
Output: data/warehouse.db  (tables + analytical views), data/exceptions.csv,
        data/reconciled_orders.csv, data/waterfall.csv
"""
import os
import sqlite3
import pandas as pd
import numpy as np

BASE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(BASE, "data", "raw")
DB = os.path.join(BASE, "data", "warehouse.db")

exceptions = []   # (entity, record_id, issue_type, severity, detail, action_taken)
log = []          # pipeline audit log (step, rows_in, rows_out, note)


def exc(entity, rid, issue, severity, detail, action):
    exceptions.append(dict(entity=entity, record_id=str(rid), issue_type=issue,
                           severity=severity, detail=detail, action_taken=action))


def load(name):
    return pd.read_csv(os.path.join(RAW, f"{name}.csv"))


# ------------------------------------------------------------------ extract
orders_raw = load("orders")
items_raw = load("order_items")
cust = load("customers")
pcost = load("product_costs")
camp = load("campaigns")
refunds = load("refunds")
deliv = load("deliveries")

# naive (uncorrected) revenue: what a plain SUM over raw item rows would report
naive_gross = float((items_raw.quantity * items_raw.unit_price).sum())
log.append(("extract", len(orders_raw), len(orders_raw), "raw orders"))

# ------------------------------------------------------------------ customers
cust["segment_raw"] = cust["segment"]
cust["segment"] = cust["segment"].str.strip().str.title().replace({"Vip": "VIP"})
cust["segment"] = cust["segment"].fillna("Unknown")
for r in cust[cust.segment_raw.fillna("") != cust.segment.where(cust.segment_raw.notna(), "")].itertuples():
    if pd.isna(r.segment_raw):
        exc("customers", r.customer_id, "missing_segment", "Low", "segment is null", "set to 'Unknown'")
    else:
        exc("customers", r.customer_id, "inconsistent_label", "Low",
            f"segment '{r.segment_raw}'", f"standardised to '{r.segment}'")
cust = cust.drop(columns="segment_raw")

# ------------------------------------------------------------------ products
prod_price = (items_raw.groupby("product_id").unit_price.median().rename("list_price").reset_index())
products = pcost.merge(prod_price, on="product_id", how="left")
products["cost_ratio"] = products.unit_cost / products.list_price
cat_ratio = products.groupby("category").cost_ratio.median()
miss = products.unit_cost.isna()
for r in products[miss].itertuples():
    est = round(r.list_price * cat_ratio[r.category], 2)
    exc("product_costs", r.product_id, "missing_cost", "Medium",
        "unit_cost is null", f"estimated {est} from category median cost ratio")
    products.loc[products.product_id == r.product_id, "unit_cost"] = est
    products.loc[products.product_id == r.product_id, "cost_estimated"] = 1
products["cost_estimated"] = products.get("cost_estimated", 0)
products["cost_estimated"] = products["cost_estimated"].fillna(0).astype(int)
products = products.drop(columns=["list_price", "cost_ratio"])

# ------------------------------------------------------------------ orders
n0 = len(orders_raw)
dups = orders_raw[orders_raw.duplicated("order_id", keep="first")]
for r in dups.itertuples():
    exc("orders", r.order_id, "duplicate_transaction", "High", "order_id repeated", "kept first occurrence")
orders = orders_raw.drop_duplicates("order_id", keep="first").copy()
log.append(("dedupe_orders", n0, len(orders), "removed duplicate order_id rows"))

# status normalisation
orders["status_raw"] = orders.order_status
orders["order_status"] = orders.order_status.str.strip().str.title().replace({"Complete": "Delivered"})
chg = orders[orders.status_raw != orders.order_status]
for r in chg.itertuples():
    exc("orders", r.order_id, "inconsistent_status_label", "Low",
        f"'{r.status_raw}'", f"standardised to '{r.order_status}'")


# dates (mixed formats)
def parse_date(s):
    if pd.isna(s):
        return pd.NaT
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return pd.to_datetime(s, format=fmt)
        except ValueError:
            continue
    return pd.NaT


orders["order_date_clean"] = orders.order_date.map(parse_date)
for r in orders[orders.order_date.notna() & orders.order_date.astype(str).str.contains("/")].itertuples():
    exc("orders", r.order_id, "date_format", "Low", f"'{r.order_date}'", "parsed as dd/mm/yyyy")
for r in orders[orders.order_date_clean.isna()].itertuples():
    exc("orders", r.order_id, "missing_date", "Medium", "order_date null",
        "kept; excluded from monthly trend")
orders["order_date"] = orders.order_date_clean
orders["month"] = orders.order_date.dt.strftime("%Y-%m")
orders = orders.drop(columns=["order_date_clean"])

# unknown customers
known = set(cust.customer_id)
for r in orders[~orders.customer_id.isin(known)].itertuples():
    exc("orders", r.order_id, "orphan_customer", "Medium", f"customer {r.customer_id} not in profiles",
        "segment set to 'Unknown'")
orders = orders.merge(cust[["customer_id", "segment", "city"]], on="customer_id", how="left")
orders["segment"] = orders.segment.fillna("Unknown")
orders["city"] = orders.city.fillna("Unknown")

# ------------------------------------------------------------------ items
m0 = len(items_raw)
idups = items_raw[items_raw.duplicated("item_id", keep="first")]
for r in idups.itertuples():
    exc("order_items", r.item_id, "duplicate_transaction", "High",
        f"item repeated in order {r.order_id}", "kept first occurrence")
items = items_raw.drop_duplicates("item_id", keep="first").copy()
log.append(("dedupe_items", m0, len(items), "removed duplicate item rows"))

bad_qty = items[items.quantity <= 0]
for r in bad_qty.itertuples():
    exc("order_items", r.item_id, "invalid_quantity", "High", f"quantity={r.quantity}", "line excluded")
items = items[items.quantity > 0]

pk = set(products.product_id)
orph = items[~items.product_id.isin(pk)]
for r in orph.itertuples():
    exc("order_items", r.item_id, "orphan_product", "High",
        f"product {r.product_id} has no cost record", "line excluded (cannot compute margin)")
items = items[items.product_id.isin(pk)]

orph_o = items[~items.order_id.isin(set(orders.order_id))]
for r in orph_o.itertuples():
    exc("order_items", r.item_id, "orphan_order", "High", f"order {r.order_id} missing", "line excluded")
items = items[items.order_id.isin(set(orders.order_id))]
log.append(("validate_items", m0, len(items), "after dedupe + validation"))

# ------------------------------------------------------------------ campaigns
camp["start_date"] = pd.to_datetime(camp.start_date)
camp["end_date"] = pd.to_datetime(camp.end_date)
orders = orders.merge(camp[["campaign_id", "campaign_name", "discount_pct", "start_date", "end_date"]],
                      on="campaign_id", how="left")
bad_c = orders[(orders.campaign_id != "CMP00") & orders.order_date.notna() &
               ((orders.order_date < orders.start_date) | (orders.order_date > orders.end_date))]
for r in bad_c.itertuples():
    exc("orders", r.order_id, "campaign_out_of_window", "Medium",
        f"{r.campaign_id} used on {r.order_date.date()}", "discount kept; flagged for review")
orders = orders.drop(columns=["start_date", "end_date"])

# ------------------------------------------------------------------ order-level money
items = items.merge(products[["product_id", "product_name", "category", "unit_cost", "handling_cost",
                              "cost_estimated"]], on="product_id", how="left")
items["line_gross"] = items.quantity * items.unit_price
items["line_cogs"] = items.quantity * items.unit_cost
items["line_handling"] = items.quantity * items.handling_cost

ot = items.groupby("order_id").agg(gross=("line_gross", "sum"), cogs=("line_cogs", "sum"),
                                   handling=("line_handling", "sum"), n_items=("item_id", "count")).reset_index()
orders = orders.merge(ot, on="order_id", how="left")
no_items = orders[orders.gross.isna()]
for r in no_items.itertuples():
    exc("orders", r.order_id, "order_without_items", "Medium", "no valid item lines", "revenue set to 0")
orders[["gross", "cogs", "handling", "n_items"]] = orders[["gross", "cogs", "handling", "n_items"]].fillna(0)
orders["discount"] = (orders.gross * orders.discount_pct / 100).round(2)
orders["net_before_refund"] = orders.gross - orders.discount

# deliveries
dd = deliv.copy()
d_dups = dd[dd.duplicated("order_id", keep="first")]
dd = dd.drop_duplicates("order_id", keep="first")
orders = orders.merge(dd[["order_id", "delivered_date", "delivery_cost", "delivery_status"]],
                      on="order_id", how="left")
orders["delivery_cost"] = orders.delivery_cost.fillna(0)
orphan_d = dd[~dd.order_id.isin(set(orders.order_id))]
for r in orphan_d.itertuples():
    exc("deliveries", r.order_id, "orphan_delivery", "Medium", "no matching order", "ignored")

# refunds
rf = refunds.copy()
orphan_r = rf[~rf.order_id.isin(set(orders.order_id))]
for r in orphan_r.itertuples():
    exc("refunds", r.refund_id, "orphan_refund", "High", f"order {r.order_id} not found",
        "ignored (unmatched refund — investigate)")
rf = rf[rf.order_id.isin(set(orders.order_id))]
ref_sum = rf.groupby("order_id").agg(refund_raw=("refund_amount", "sum"),
                                     refund_types=("refund_type", lambda s: ",".join(sorted(set(s))))).reset_index()
orders = orders.merge(ref_sum, on="order_id", how="left")
orders["refund_raw"] = orders.refund_raw.fillna(0)
orders["refund_types"] = orders.refund_types.fillna("")

# cap refunds at what the customer actually paid for items
over = orders[orders.refund_raw > orders.net_before_refund + 0.01]
for r in over.itertuples():
    exc("refunds", r.order_id, "refund_exceeds_order_value", "High",
        f"refund {r.refund_raw:.2f} > net order value {r.net_before_refund:.2f}", "capped at net order value")
orders["refund"] = np.minimum(orders.refund_raw, orders.net_before_refund).round(2)

# ------------------------------------------------------------------ reconcile outcome
# Rules (documented in README):
#  1. Cancelled                        -> Cancelled, no revenue.  (delivery record present => conflict flagged,
#                                          delivery cost kept as pure leakage)
#  2. Full refund (>=99% of net)       -> Returned (overrides a 'Delivered' label)
#  3. Partial refund on Delivered      -> Partially Refunded
#  4. Delivered / Returned labels      -> as stated
#  5. Shipped                          -> In Transit (revenue not yet recognised)
def outcome(r):
    full = r.net_before_refund > 0 and r.refund >= 0.99 * r.net_before_refund
    if r.order_status == "Cancelled":
        return "Cancelled"
    if r.order_status == "Shipped":
        return "In Transit"
    if full or r.order_status == "Returned":
        return "Returned"
    if r.refund > 0:
        return "Partially Refunded"
    return "Delivered"


orders["final_status"] = orders.apply(outcome, axis=1)

# conflicts
c1 = orders[(orders.order_status == "Cancelled") & (orders.delivery_status == "Delivered")]
for r in c1.itertuples():
    exc("orders", r.order_id, "status_conflict", "High", "marked Cancelled but delivery record says Delivered",
        "treated as Cancelled; delivery cost counted as leakage")
c2 = orders[(orders.order_status == "Delivered") & (orders.final_status == "Returned") & (orders.refund > 0)]
for r in c2.itertuples():
    exc("orders", r.order_id, "status_conflict", "High", "marked Delivered but fully refunded",
        "reclassified as Returned")
c3 = orders[(orders.order_status == "Returned") & (orders.refund == 0)]
for r in c3.itertuples():
    exc("orders", r.order_id, "returned_without_refund", "Medium", "Returned but no refund record",
        "treated as Returned with full refund assumed")
c4 = orders[(orders.order_status == "Delivered") & (orders.delivery_status == "In Transit")]
for r in c4.itertuples():
    exc("orders", r.order_id, "status_conflict", "Medium", "Delivered but delivery shows In Transit", "kept Delivered")

# returned with no refund record -> assume full refund
mask = (orders.final_status == "Returned") & (orders.refund == 0)
orders.loc[mask, "refund"] = orders.loc[mask, "net_before_refund"]

recognised = orders.final_status.isin(["Delivered", "Partially Refunded", "Returned"])
orders["recognised"] = recognised.astype(int)
orders["net_revenue"] = np.where(recognised, orders.net_before_refund - orders.refund, 0).round(2)
orders["shipping_income"] = np.where(recognised & (orders.final_status != "Returned"), orders.shipping_fee, 0)
# full returns assumed restocked => COGS 0; partial refunds => goods kept, COGS full
orders["cogs_recognised"] = np.where(recognised & (orders.final_status != "Returned"), orders.cogs, 0).round(2)
orders["handling_recognised"] = np.where(recognised, orders.handling, 0).round(2)
# delivery cost is incurred whenever goods left the warehouse (incl. cancelled-but-delivered conflicts)
orders["delivery_recognised"] = np.where(orders.final_status == "In Transit", 0, orders.delivery_cost)
orders["contribution"] = (orders.net_revenue + orders.shipping_income - orders.cogs_recognised
                          - orders.handling_recognised - orders.delivery_recognised).round(2)
orders["dq_flag"] = orders.order_id.isin({e["record_id"] for e in exceptions}).astype(int)

# ------------------------------------------------------------------ item-level fact (allocation)
f = items.merge(orders[["order_id", "customer_id", "segment", "campaign_id", "campaign_name", "month",
                        "final_status", "recognised", "gross", "discount_pct", "net_before_refund", "refund",
                        "shipping_income", "delivery_recognised"]], on="order_id", how="left")
f["disc_amt"] = f.line_gross * f.discount_pct / 100
f["line_net_before_refund"] = f.line_gross - f.disc_amt
share = np.where(f.gross > 0, f.line_gross / f.gross, 0)
f["line_refund"] = (f.refund * share).round(2)
rec = f.recognised == 1
f["net_revenue"] = np.where(rec, f.line_net_before_refund - f.line_refund, 0).round(2)
keep_cogs = rec & (f.final_status != "Returned")
f["cogs"] = np.where(keep_cogs, f.line_cogs, 0).round(2)
f["handling"] = np.where(rec, f.line_handling, 0).round(2)
f["delivery_alloc"] = (f.delivery_recognised * share).round(2)
f["shipping_alloc"] = (f.shipping_income * share).round(2)
f["contribution"] = (f.net_revenue + f.shipping_alloc - f.cogs - f.handling - f.delivery_alloc).round(2)
fact_item = f[["item_id", "order_id", "product_id", "product_name", "category", "customer_id", "segment",
               "campaign_id", "campaign_name", "month", "final_status", "quantity", "unit_price", "line_gross",
               "disc_amt", "line_refund", "net_revenue", "cogs", "handling", "delivery_alloc", "shipping_alloc",
               "contribution", "cost_estimated"]].copy()

# ------------------------------------------------------------------ leakage waterfall (naive -> reconciled)
dup_item_value = float((items_raw[items_raw.duplicated("item_id", keep="first")].eval("quantity*unit_price")).sum())
items_dedup = items_raw.drop_duplicates("item_id")
invalid = items_dedup[(items_dedup.quantity <= 0) | (~items_dedup.product_id.isin(pk))]
invalid_value = float((invalid.quantity * invalid.unit_price).sum())
valid_gross = float(items.line_gross.sum())
cancel_gross = float(orders[orders.final_status == "Cancelled"].gross.sum())
transit_gross = float(orders[orders.final_status == "In Transit"].gross.sum())
rec_orders = orders[orders.recognised == 1]
disc_total = float(rec_orders.discount.sum())
ref_total = float(rec_orders.refund.sum())
net_total = float(orders.net_revenue.sum())
waterfall = pd.DataFrame([
    ("Naive gross (raw SUM of item rows)", naive_gross),
    ("- Duplicate item rows", -dup_item_value),
    ("- Invalid / orphan lines", -(invalid_value) if True else 0),
    ("- Cancelled orders", -cancel_gross),
    ("- In-transit orders (not yet recognised)", -transit_gross),
    ("- Discounts / campaigns", -disc_total),
    ("- Refunds", -ref_total),
    ("= Reconciled net revenue", net_total),
], columns=["step", "amount"])
chk = naive_gross - dup_item_value - invalid_value - cancel_gross - transit_gross - disc_total - ref_total
assert abs(chk - net_total) < 1.0, f"waterfall does not reconcile: {chk} vs {net_total}"

# ------------------------------------------------------------------ load to SQLite
os.makedirs(os.path.dirname(DB), exist_ok=True)
if os.path.exists(DB):
    os.remove(DB)
con = sqlite3.connect(DB)
order_cols = ["order_id", "customer_id", "segment", "city", "order_date", "month", "campaign_id", "campaign_name",
              "discount_pct", "payment_method", "status_raw", "order_status", "final_status", "recognised",
              "n_items", "gross", "discount", "net_before_refund", "refund", "net_revenue", "shipping_income",
              "cogs_recognised", "handling_recognised", "delivery_recognised", "contribution", "dq_flag"]
fo = orders[order_cols].copy()
fo["order_date"] = fo.order_date.dt.strftime("%Y-%m-%d")
fo.to_sql("fact_order", con, index=False)
fact_item.to_sql("fact_order_item", con, index=False)
cust.to_sql("dim_customer", con, index=False)
products.to_sql("dim_product", con, index=False)
camp.assign(start_date=camp.start_date.dt.strftime("%Y-%m-%d"),
            end_date=camp.end_date.dt.strftime("%Y-%m-%d")).to_sql("dim_campaign", con, index=False)
ex_df = pd.DataFrame(exceptions)
ex_df.to_sql("dq_exceptions", con, index=False)
waterfall.to_sql("revenue_waterfall", con, index=False)
pd.DataFrame(log, columns=["step", "rows_in", "rows_out", "note"]).to_sql("pipeline_log", con, index=False)

con.executescript("""
CREATE INDEX ix_item_order ON fact_order_item(order_id);
CREATE INDEX ix_item_product ON fact_order_item(product_id);
CREATE INDEX ix_order_cust ON fact_order(customer_id);

CREATE VIEW v_product_summary AS
SELECT product_id, product_name, category,
       SUM(quantity) AS units,
       ROUND(SUM(line_gross),2) AS gross_sales,
       ROUND(SUM(disc_amt),2) AS discounts,
       ROUND(SUM(line_refund),2) AS refunds,
       ROUND(SUM(net_revenue),2) AS net_revenue,
       ROUND(SUM(contribution),2) AS contribution,
       ROUND(100.0*SUM(contribution)/NULLIF(SUM(net_revenue),0),1) AS margin_pct,
       MAX(cost_estimated) AS cost_estimated
FROM fact_order_item GROUP BY product_id;

CREATE VIEW v_segment_summary AS
SELECT segment, COUNT(*) AS orders, ROUND(SUM(gross),2) AS gross_sales,
       ROUND(SUM(discount),2) AS discounts, ROUND(SUM(refund),2) AS refunds,
       ROUND(SUM(net_revenue),2) AS net_revenue, ROUND(SUM(contribution),2) AS contribution,
       ROUND(100.0*SUM(contribution)/NULLIF(SUM(net_revenue),0),1) AS margin_pct,
       ROUND(100.0*SUM(CASE WHEN final_status='Returned' THEN 1 ELSE 0 END)/COUNT(*),1) AS return_rate_pct
FROM fact_order GROUP BY segment;

CREATE VIEW v_campaign_summary AS
SELECT campaign_id, campaign_name, MAX(discount_pct) AS discount_pct, COUNT(*) AS orders,
       ROUND(SUM(gross),2) AS gross_sales, ROUND(SUM(discount),2) AS discounts,
       ROUND(SUM(refund),2) AS refunds, ROUND(SUM(net_revenue),2) AS net_revenue,
       ROUND(SUM(contribution),2) AS contribution,
       ROUND(100.0*SUM(contribution)/NULLIF(SUM(net_revenue),0),1) AS margin_pct,
       ROUND(100.0*SUM(CASE WHEN final_status='Returned' THEN 1 ELSE 0 END)/COUNT(*),1) AS return_rate_pct
FROM fact_order WHERE recognised=1 OR final_status='Cancelled' GROUP BY campaign_id;

CREATE VIEW v_customer_summary AS
SELECT customer_id, segment, COUNT(*) AS orders, ROUND(SUM(gross),2) AS gross_sales,
       ROUND(SUM(net_revenue),2) AS net_revenue, ROUND(SUM(contribution),2) AS contribution,
       ROUND(100.0*SUM(contribution)/NULLIF(SUM(net_revenue),0),1) AS margin_pct,
       SUM(CASE WHEN final_status='Returned' THEN 1 ELSE 0 END) AS returns
FROM fact_order WHERE customer_id IN (SELECT customer_id FROM dim_customer) GROUP BY customer_id;

CREATE VIEW v_monthly AS
SELECT month, ROUND(SUM(gross),2) AS gross_sales, ROUND(SUM(net_revenue),2) AS net_revenue,
       ROUND(SUM(contribution),2) AS contribution
FROM fact_order WHERE month IS NOT NULL GROUP BY month ORDER BY month;
""")
con.commit()
con.close()

# CSV exports (auditable artefacts)
os.makedirs(os.path.join(BASE, "output"), exist_ok=True)
fo.to_csv(os.path.join(BASE, "output", "reconciled_orders.csv"), index=False)
ex_df.to_csv(os.path.join(BASE, "output", "exception_report.csv"), index=False)
waterfall.to_csv(os.path.join(BASE, "output", "revenue_waterfall.csv"), index=False)

print("ETL complete.")
print(f"  orders: {len(orders_raw)} raw -> {len(orders)} clean | items: {len(items_raw)} raw -> {len(items)} clean")
print(f"  exceptions logged: {len(ex_df)}")
print(ex_df.groupby("issue_type").size().sort_values(ascending=False).to_string())
print(waterfall.to_string(index=False))
