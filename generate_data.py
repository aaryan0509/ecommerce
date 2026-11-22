"""Generate a mock e-commerce dataset with realistic data-quality defects.
Outputs CSVs into ./data/raw. Seeded so results are reproducible."""
import random, os
from datetime import date, timedelta
import pandas as pd

random.seed(42)
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "raw")
os.makedirs(OUT, exist_ok=True)

# ---------- products & costs ----------
cats = {"Electronics": (800, 5000), "Fashion": (300, 2500), "Home": (400, 3500),
        "Beauty": (150, 1500), "Sports": (500, 4000)}
products = []
pid = 1
for c, (lo, hi) in cats.items():
    for i in range(8):
        price = round(random.uniform(lo, hi), -1)
        cost = round(price * random.uniform(0.45, 0.85), 2)
        products.append(dict(product_id=f"P{pid:03d}", product_name=f"{c} Item {i+1}",
                             category=c, unit_price=price, unit_cost=cost,
                             handling_cost=round(random.uniform(20, 60), 2)))
        pid += 1
prod = pd.DataFrame(products)
pc = prod[["product_id", "product_name", "category", "unit_cost", "handling_cost"]].copy()
# defect: a few missing costs
pc.loc[random.sample(range(len(pc)), 2), "unit_cost"] = None

# ---------- customers ----------
segs = ["New", "Regular", "Loyal", "VIP"]
cities = ["Mumbai", "Pune", "Nashik", "Delhi", "Bengaluru", "Chennai", "Kolkata"]
cust = pd.DataFrame([dict(customer_id=f"C{i:04d}",
                          name=f"Customer {i}",
                          city=random.choice(cities),
                          segment=random.choices(segs, [40, 35, 18, 7])[0])
                     for i in range(1, 301)])
# defect: inconsistent segment labels / missing
cust.loc[random.sample(range(300), 12), "segment"] = "vip "
cust.loc[random.sample(range(300), 10), "segment"] = "regular"
cust.loc[random.sample(range(300), 8), "segment"] = None

# ---------- campaigns ----------
camp = pd.DataFrame([
    dict(campaign_id="CMP00", campaign_name="No Campaign", discount_pct=0, start_date="2026-01-01", end_date="2026-12-31"),
    dict(campaign_id="CMP01", campaign_name="New Year Sale", discount_pct=10, start_date="2026-01-01", end_date="2026-01-15"),
    dict(campaign_id="CMP02", campaign_name="Summer Clearance", discount_pct=25, start_date="2026-04-01", end_date="2026-05-15"),
    dict(campaign_id="CMP03", campaign_name="Festive Blast", discount_pct=30, start_date="2026-08-15", end_date="2026-09-30"),
    dict(campaign_id="CMP04", campaign_name="Loyalty Week", discount_pct=15, start_date="2026-06-01", end_date="2026-06-07"),
    dict(campaign_id="CMP05", campaign_name="Flash 40", discount_pct=40, start_date="2026-10-01", end_date="2026-10-05"),
])

# ---------- orders / items ----------
start = date(2026, 1, 1)
orders, items, deliveries, refunds = [], [], [], []
rid = 1
item_id = 1
for o in range(1, 2001):
    oid = f"O{o:05d}"
    d = start + timedelta(days=random.randint(0, 313))
    c = random.choice(cust.customer_id.tolist())
    # pick campaign active on date (or none)
    active = [r for r in camp.itertuples() if r.campaign_id != "CMP00" and r.start_date <= d.isoformat() <= r.end_date]
    cmp_id = random.choice(active).campaign_id if active and random.random() < 0.8 else "CMP00"
    status = random.choices(["Delivered", "Cancelled", "Returned", "Shipped"], [78, 8, 9, 5])[0]
    n_items = random.choices([1, 2, 3, 4], [50, 30, 15, 5])[0]
    chosen = random.sample(products, n_items)
    gross = 0
    for p in chosen:
        q = random.choices([1, 2, 3], [70, 22, 8])[0]
        items.append(dict(item_id=f"I{item_id:06d}", order_id=oid, product_id=p["product_id"],
                          quantity=q, unit_price=p["unit_price"]))
        item_id += 1
        gross += q * p["unit_price"]
    ship_fee = random.choice([0, 49, 79, 99])
    orders.append(dict(order_id=oid, customer_id=c, order_date=d.isoformat(),
                       campaign_id=cmp_id, order_status=status, shipping_fee=ship_fee,
                       payment_method=random.choice(["UPI", "Card", "COD", "NetBanking"])))
    # deliveries
    if status in ("Delivered", "Returned", "Shipped"):
        dd = d + timedelta(days=random.randint(2, 9))
        deliveries.append(dict(order_id=oid, shipped_date=(d + timedelta(days=1)).isoformat(),
                               delivered_date=dd.isoformat() if status != "Shipped" else None,
                               delivery_cost=round(random.uniform(40, 120), 2),
                               delivery_status="Delivered" if status != "Shipped" else "In Transit"))
    # refunds
    disc = camp.set_index("campaign_id").loc[cmp_id, "discount_pct"]
    net_items = gross * (1 - disc / 100)
    if status == "Returned":
        refunds.append(dict(refund_id=f"R{rid:05d}", order_id=oid, refund_amount=round(net_items, 2),
                            refund_type="Full", refund_date=(d + timedelta(days=12)).isoformat()))
        rid += 1
    elif status == "Delivered" and random.random() < 0.08:
        refunds.append(dict(refund_id=f"R{rid:05d}", order_id=oid,
                            refund_amount=round(net_items * random.uniform(0.1, 0.5), 2),
                            refund_type="Partial", refund_date=(d + timedelta(days=10)).isoformat()))
        rid += 1

orders = pd.DataFrame(orders); items = pd.DataFrame(items)
deliveries = pd.DataFrame(deliveries); refunds = pd.DataFrame(refunds)

# ---------- inject defects ----------
# 1. duplicate transactions (exact duplicate order rows + duplicate items)
dup_o = orders.sample(40, random_state=1)
orders = pd.concat([orders, dup_o], ignore_index=True)
dup_i = items.sample(60, random_state=2)
items = pd.concat([items, dup_i], ignore_index=True)
# 2. inconsistent status labels
m = orders.sample(60, random_state=3).index
orders.loc[m, "order_status"] = orders.loc[m, "order_status"].str.upper()
m = orders.sample(25, random_state=4).index
orders.loc[m, "order_status"] = "Complete"   # synonym of Delivered
# 3. status contradicts refunds: Delivered orders with full refund, Cancelled with delivery
full_ref = refunds[refunds.refund_type == "Full"].order_id.sample(15, random_state=5)
orders.loc[orders.order_id.isin(full_ref), "order_status"] = "Delivered"
canc = orders[orders.order_status == "Cancelled"].order_id.sample(10, random_state=6)
extra = pd.DataFrame([dict(order_id=o, shipped_date="2026-03-01", delivered_date="2026-03-05",
                           delivery_cost=80.0, delivery_status="Delivered") for o in canc])
deliveries = pd.concat([deliveries, extra], ignore_index=True)
# 4. orphan / bad keys
items.loc[items.sample(8, random_state=7).index, "product_id"] = "P999"
orders.loc[orders.sample(6, random_state=8).index, "customer_id"] = "C9999"
# 5. refund greater than order value; negative qty; missing dates; mixed date formats
refunds.loc[refunds.sample(5, random_state=9).index, "refund_amount"] *= 3
items.loc[items.sample(6, random_state=10).index, "quantity"] = -1
orders.loc[orders.sample(10, random_state=11).index, "order_date"] = None
idx = orders.sample(30, random_state=12).index
orders.loc[idx, "order_date"] = pd.to_datetime(orders.loc[idx, "order_date"]).dt.strftime("%d/%m/%Y")
# 6. refund with no matching order
refunds = pd.concat([refunds, pd.DataFrame([dict(refund_id="R90001", order_id="O99999",
                     refund_amount=500.0, refund_type="Partial", refund_date="2026-05-05")])], ignore_index=True)

orders = orders.sample(frac=1, random_state=99).reset_index(drop=True)
for name, df in dict(orders=orders, order_items=items, customers=cust, product_costs=pc,
                     campaigns=camp, refunds=refunds, deliveries=deliveries).items():
    df.to_csv(os.path.join(OUT, f"{name}.csv"), index=False)
    print(f"{name}.csv: {len(df)} rows")
