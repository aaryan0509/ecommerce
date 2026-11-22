"""Revenue Leakage & Customer Profitability dashboard.
Run:  streamlit run app.py
"""
import os
import re
import sqlite3
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "warehouse.db")
st.set_page_config(page_title="Revenue Leakage Analyzer", layout="wide")


@st.cache_data
def q(sql, params=()):
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        return pd.read_sql(sql, con, params=params)
    finally:
        con.close()


if not os.path.exists(DB):
    st.error("Warehouse not found. Run `python generate_data.py` then `python etl.py` first.")
    st.stop()

inr = lambda v: f"₹{v:,.0f}"

st.title("E-Commerce Revenue Leakage & Customer Profitability")
st.caption("Reconciled from orders, items, customers, product costs, campaigns, refunds and deliveries.")

# ---------------- sidebar filters
orders_all = q("SELECT * FROM fact_order")
st.sidebar.header("Filters")
months = sorted(orders_all.month.dropna().unique())
m_lo, m_hi = st.sidebar.select_slider("Month range", options=months, value=(months[0], months[-1]))
segs = st.sidebar.multiselect("Segment", sorted(orders_all.segment.unique()), default=sorted(orders_all.segment.unique()))
camps = st.sidebar.multiselect("Campaign", sorted(orders_all.campaign_name.unique()),
                               default=sorted(orders_all.campaign_name.unique()))
fo = orders_all[(orders_all.month.between(m_lo, m_hi) | orders_all.month.isna())
                & orders_all.segment.isin(segs) & orders_all.campaign_name.isin(camps)]
fi = q("SELECT * FROM fact_order_item")
fi = fi[fi.order_id.isin(fo.order_id)]

tabs = st.tabs(["Overview", "Products", "Customers & Segments", "Campaigns", "Order Explorer",
                "Data Quality", "SQL Console"])

# ---------------- Overview
with tabs[0]:
    rec = fo[fo.recognised == 1]
    net, contrib = fo.net_revenue.sum(), fo.contribution.sum()
    c = st.columns(5)
    c[0].metric("Net revenue", inr(net))
    c[1].metric("Contribution", inr(contrib), f"{100*contrib/net:.1f}% margin" if net else None)
    c[2].metric("Discounts given", inr(rec.discount.sum()))
    c[3].metric("Refunds", inr(rec.refund.sum()))
    c[4].metric("Return rate", f"{100*(fo.final_status=='Returned').sum()/max(len(fo),1):.1f}%")

    left, right = st.columns(2)
    wf = q("SELECT * FROM revenue_waterfall")
    with left:
        st.subheader("From naive revenue to reconciled net revenue")
        st.caption("Whole dataset (not filtered). Each bar is revenue removed by one data problem or leakage.")
        meas = ["absolute"] + ["relative"] * (len(wf) - 2) + ["total"]
        fig = go.Figure(go.Waterfall(x=wf.step, y=wf.amount, measure=meas,
                                     connector={"line": {"color": "#999"}}))
        fig.update_layout(height=430, margin=dict(l=0, r=0, t=10, b=0), xaxis_tickangle=-30)
        st.plotly_chart(fig, width="stretch")
    with right:
        st.subheader("Monthly net revenue vs contribution")
        mm = fo.dropna(subset=["month"]).groupby("month")[["net_revenue", "contribution"]].sum().reset_index()
        fig = px.line(mm.melt("month"), x="month", y="value", color="variable", markers=True)
        fig.update_layout(height=430, margin=dict(l=0, r=0, t=10, b=0), legend_title=None)
        st.plotly_chart(fig, width="stretch")

    st.subheader("Order outcome mix (reconciled)")
    mix = fo.groupby("final_status").agg(orders=("order_id", "count"), gross=("gross", "sum"),
                                         net_revenue=("net_revenue", "sum")).reset_index()
    st.dataframe(mix, width="stretch", hide_index=True)

# ---------------- Products
with tabs[1]:
    ps = fi.groupby(["product_id", "product_name", "category"]).agg(
        units=("quantity", "sum"), gross_sales=("line_gross", "sum"), discounts=("disc_amt", "sum"),
        refunds=("line_refund", "sum"), net_revenue=("net_revenue", "sum"),
        contribution=("contribution", "sum")).reset_index()
    ps["margin_pct"] = (100 * ps.contribution / ps.net_revenue.where(ps.net_revenue != 0)).round(1)
    ps = ps.sort_values("contribution", ascending=False).reset_index(drop=True)
    ps.insert(0, "rank", ps.index + 1)
    a, b = st.columns(2)
    with a:
        st.subheader("Top 10 by contribution")
        st.plotly_chart(px.bar(ps.head(10), x="contribution", y="product_name", orientation="h",
                               color="category").update_layout(yaxis=dict(autorange="reversed"), height=400),
                        width="stretch")
    with b:
        st.subheader("Bottom 10 (profit leakers)")
        st.plotly_chart(px.bar(ps.tail(10), x="contribution", y="product_name", orientation="h",
                               color="category").update_layout(yaxis=dict(autorange="reversed"), height=400),
                        width="stretch")
    st.subheader("Ranked product summary")
    st.dataframe(ps, width="stretch", hide_index=True)
    st.download_button("Download CSV", ps.to_csv(index=False), "product_summary.csv")
    pick = st.selectbox("Inspect supporting lines for product", ps.product_name)
    st.dataframe(fi[fi.product_name == pick][["item_id", "order_id", "month", "final_status", "quantity",
                                              "line_gross", "disc_amt", "line_refund", "net_revenue", "cogs",
                                              "handling", "delivery_alloc", "contribution"]],
                 width="stretch", hide_index=True)

# ---------------- Customers & segments
with tabs[2]:
    seg = fo.groupby("segment").agg(orders=("order_id", "count"), net_revenue=("net_revenue", "sum"),
                                    contribution=("contribution", "sum"), refunds=("refund", "sum"),
                                    returned=("final_status", lambda s: (s == "Returned").sum())).reset_index()
    seg["margin_pct"] = (100 * seg.contribution / seg.net_revenue).round(1)
    seg["return_rate_pct"] = (100 * seg.returned / seg.orders).round(1)
    seg["contribution_per_order"] = (seg.contribution / seg.orders).round(0)
    a, b = st.columns(2)
    a.plotly_chart(px.bar(seg, x="segment", y=["net_revenue", "contribution"], barmode="group",
                          title="Revenue vs contribution by segment"), width="stretch")
    b.plotly_chart(px.bar(seg, x="segment", y="return_rate_pct", title="Return rate % by segment"),
                   width="stretch")
    st.dataframe(seg, width="stretch", hide_index=True)
    cs = fo[fo.customer_id.isin(q("SELECT customer_id FROM dim_customer").customer_id)].groupby(
        ["customer_id", "segment"]).agg(orders=("order_id", "count"), net_revenue=("net_revenue", "sum"),
                                         contribution=("contribution", "sum"),
                                         returns=("final_status", lambda s: (s == "Returned").sum())
                                         ).reset_index().sort_values("contribution", ascending=False)
    x, y = st.columns(2)
    x.subheader("Top 15 customers by contribution")
    x.dataframe(cs.head(15), width="stretch", hide_index=True)
    y.subheader("Bottom 15 (unprofitable customers)")
    y.dataframe(cs.tail(15).sort_values("contribution"), width="stretch", hide_index=True)

# ---------------- Campaigns
with tabs[3]:
    cp = fo[fo.recognised.eq(1) | fo.final_status.eq("Cancelled")].groupby(["campaign_name", "discount_pct"]).agg(
        orders=("order_id", "count"), gross_sales=("gross", "sum"), discounts=("discount", "sum"),
        refunds=("refund", "sum"), net_revenue=("net_revenue", "sum"),
        contribution=("contribution", "sum")).reset_index()
    cp["margin_pct"] = (100 * cp.contribution / cp.net_revenue).round(1)
    cp["contribution_per_order"] = (cp.contribution / cp.orders).round(0)
    cp = cp.sort_values("contribution", ascending=False)
    a, b = st.columns(2)
    a.plotly_chart(px.bar(cp, x="campaign_name", y="margin_pct", color="discount_pct",
                          title="Contribution margin % by campaign"), width="stretch")
    b.plotly_chart(px.scatter(cp, x="discount_pct", y="contribution_per_order", size="orders",
                              text="campaign_name", title="Deeper discount vs contribution per order"),
                   width="stretch")
    st.dataframe(cp, width="stretch", hide_index=True)

# ---------------- Order explorer
with tabs[4]:
    st.write("Search any order to see how its outcome was reconstructed.")
    oid = st.text_input("Order ID (e.g. O00001)")
    status_f = st.multiselect("Or filter by reconciled status", sorted(fo.final_status.unique()))
    only_flag = st.checkbox("Only orders with a data-quality flag")
    view = fo
    if status_f:
        view = view[view.final_status.isin(status_f)]
    if only_flag:
        view = view[view.dq_flag == 1]
    if oid:
        view = view[view.order_id.str.contains(oid.strip(), case=False)]
    st.dataframe(view.head(500), width="stretch", hide_index=True)
    st.caption(f"{len(view):,} orders match (showing up to 500)")
    if oid and len(view) == 1:
        r = view.iloc[0]
        st.subheader(f"Reconciliation for {r.order_id}")
        st.write(f"Raw status **{r.status_raw}** → reconciled status **{r.final_status}**")
        recon = pd.DataFrame({"component": ["Gross", "Discount", "Refund", "Net revenue", "Shipping income",
                                            "COGS", "Handling", "Delivery cost", "Contribution"],
                              "amount": [r.gross, -r.discount, -r.refund, r.net_revenue, r.shipping_income,
                                         -r.cogs_recognised, -r.handling_recognised, -r.delivery_recognised,
                                         r.contribution]})
        st.table(recon)
        st.write("Item lines")
        st.dataframe(fi[fi.order_id == r.order_id], width="stretch", hide_index=True)
        st.write("Exceptions for this order")
        st.dataframe(q("SELECT * FROM dq_exceptions WHERE record_id = ?", (r.order_id,)),
                     width="stretch", hide_index=True)

# ---------------- Data quality
with tabs[5]:
    ex = q("SELECT * FROM dq_exceptions")
    c = st.columns(3)
    c[0].metric("Total exceptions", len(ex))
    c[1].metric("High severity", int((ex.severity == "High").sum()))
    c[2].metric("Distinct issue types", ex.issue_type.nunique())
    s = ex.groupby(["issue_type", "severity"]).size().reset_index(name="count").sort_values("count", ascending=False)
    st.plotly_chart(px.bar(s, x="count", y="issue_type", color="severity", orientation="h",
                           color_discrete_map={"High": "#d62728", "Medium": "#ff7f0e", "Low": "#7f7f7f"}
                           ).update_layout(yaxis=dict(autorange="reversed")), width="stretch")
    sev = st.multiselect("Severity", ["High", "Medium", "Low"], default=["High", "Medium"])
    typ = st.multiselect("Issue type", sorted(ex.issue_type.unique()))
    v = ex[ex.severity.isin(sev)]
    if typ:
        v = v[v.issue_type.isin(typ)]
    st.dataframe(v, width="stretch", hide_index=True)
    st.download_button("Download exception report", ex.to_csv(index=False), "exception_report.csv")
    st.subheader("Pipeline audit log")
    st.dataframe(q("SELECT * FROM pipeline_log"), width="stretch", hide_index=True)

# ---------------- SQL console
with tabs[6]:
    st.write("Query the warehouse (read-only). Tables: `fact_order`, `fact_order_item`, `dim_customer`, "
             "`dim_product`, `dim_campaign`, `dq_exceptions`; views: `v_product_summary`, `v_segment_summary`, "
             "`v_campaign_summary`, `v_customer_summary`, `v_monthly`.")
    sql = st.text_area("SQL", "SELECT * FROM v_campaign_summary ORDER BY contribution DESC", height=120)
    if st.button("Run"):
        if not re.match(r"^\s*(select|with)\b", sql, re.I) or ";" in sql.strip().rstrip(";"):
            st.error("Only a single SELECT statement is allowed.")
        else:
            try:
                st.dataframe(q(sql), width="stretch", hide_index=True)
            except Exception as e:
                st.error(str(e))
