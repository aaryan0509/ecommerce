# Project Report: E-Commerce Revenue Leakage and Customer Profitability Analyzer

## 1. Problem and objective

An online retailer keeps orders, order items, customer profiles, product costs, discount campaigns, refunds and delivery records in separate files. Cancellations, partial refunds, duplicate transactions and inconsistent statuses make the true sales figure and product profitability unclear. The objective is to consolidate these records, reconstruct each order's real outcome, and calculate net revenue and estimated contribution by product, customer segment and campaign, with an auditable trail.

## 2. Data

A seeded generator (`generate_data.py`) produces seven CSV files modelling Jan to Nov 2026: 2,000 orders (2,040 rows after injected duplicates), about 3,500 item lines, 300 customers, 40 products across 5 categories, 6 campaigns (including "No Campaign"), 296 refunds and about 1,800 delivery records. Defects were injected deliberately and realistically (see section 4) so the cleaning logic can be demonstrated and measured. Because the data is synthetic, the findings illustrate the method; they are not claims about a real business.

## 3. Design

| Stage | Tool | Output |
|---|---|---|
| Extract | pandas reads the 7 CSVs | raw frames |
| Clean | dedupe, standardise labels and dates, validate keys | clean frames plus exception log |
| Reconcile | rules combine order status, refunds and deliveries into a final outcome | `final_status` per order |
| Calculate | net revenue, COGS, handling, delivery, contribution; item-level allocation | `fact_order`, `fact_order_item` |
| Store | SQLite warehouse with indexes and five analytical SQL views | `data/warehouse.db` |
| Present | Streamlit and Plotly dashboard, read-only SQL console | `app.py` |

The model is a small star schema: two fact tables (order grain and order-item grain) and three dimensions (customer, product, campaign).

## 4. Data-quality results

The pipeline logged 313 exceptions (153 High, 26 Medium, 134 Low). Main categories:

* 100 duplicate transactions removed (40 orders and 60 item lines).
* 82 order-status labels standardised (case differences and "Complete" meaning Delivered).
* 30 status conflicts: Delivered orders that were fully refunded were reclassified as Returned, and Cancelled orders that have a delivery record were kept as Cancelled with the delivery cost counted as leakage.
* 8 refunds exceeding the order value (capped), 8 lines for a product with no cost record, 6 orphan customers, 6 invalid quantities, 10 missing and 30 non-ISO dates, 2 missing product costs (estimated), 1 refund with no matching order.

## 5. Revenue reconciliation

| Step | Amount (₹) |
|---|---|
| Naive gross (plain sum of raw item rows) | 9,679,900 |
| Less duplicate item rows | -150,620 |
| Less invalid / orphan lines | -21,970 |
| Less cancelled orders | -784,920 |
| Less in-transit orders (not yet recognised) | -428,980 |
| Less discounts | -633,597 |
| Less refunds | -882,984 |
| **Reconciled net revenue** | **6,776,829** |
| **Estimated contribution** | **1,994,896 (29.4% of net revenue)** |

A naive sum overstates revenue by about 43% (₹2.9 million). Only about 70% of the naive figure is real, recognised, post-discount, post-refund revenue.

## 6. Summary measures and findings

**Measure 1: contribution margin by campaign.**

| Campaign | Discount | Margin |
|---|---|---|
| No Campaign | 0% | 34.8% |
| New Year Sale | 10% | 28.0% |
| Loyalty Week | 15% | 20.6% |
| Summer Clearance | 25% | 14.4% |
| Festive Blast | 30% | 5.3% |
| Flash 40 | 40% | -0.5% |

Margin falls steadily as discount depth rises. Festive Blast generated ₹344k of discounts and ₹643k of net revenue but only ₹34k of contribution, and Flash 40 lost money after refunds and delivery costs. Deep campaigns buy revenue, not profit.

**Measure 2: product contribution and margin.** Electronics earns 39.0% margin and ₹737k contribution on 28% of net revenue; every other category sits at 25 to 27%. The top three products (Electronics Items 3, 7 and 1) are all electronics with 44 to 48% margins. At the bottom, Beauty Item 7 (-0.2%), Beauty Item 2 (0.3%) and Fashion Item 2 (0.7%) produce almost no contribution despite ₹22k to ₹57k of net revenue each, so they are candidates for repricing or cost review. Sports Item 5 brings in ₹172k of net revenue but only a 3.3% margin.

**Measure 3: return and refund leakage by segment.** Return rates are highest for the Unknown segment (15.5%) and VIP (11.4%), against 7.1% for Regular and 7.9% for Loyal. Refunds total ₹883k, of which ₹729k is full returns. Partial refunds (121 orders, ₹382k net revenue) leave only ₹1.9k contribution because the goods are kept (full COGS) while 10 to 50% of the price is returned. Full returns also lose handling and delivery cost (about ₹14k in this sample).

**Segment profitability.** Margins are similar across segments (28.6 to 30.7%), so profitability differences come mostly from returns and campaign mix rather than from segment itself. Six of 300 customers are unprofitable overall. The "Unknown" segment (58 orders) shows that missing profile data hides about ₹169k of revenue from segment analysis.

**Interpretation.** Leakage comes from four sources: data errors that inflate reported sales (duplicates), orders that never complete (cancellations and in-transit), price concessions (discounts, ₹634k, 6.5% of naive gross) and refunds (₹883k, 9.1%). The first can be fixed by validation, while the other three are business levers. Practical recommendations: cap discounts at about 25%, review the Beauty and Fashion low-margin items, investigate partial-refund approval practice, and enforce a single order-status source so conflicts stop occurring.

## 7. Auditability

* Every exception is stored with entity, record ID, issue type, severity, detail and action taken, and can be downloaded.
* The Order Explorer shows the raw label, reconciled status, the revenue-to-contribution bridge, item lines and exceptions for any order.
* Product, campaign and segment totals can be drilled down to item lines, and the SQL console allows independent verification.
* A pipeline log records row counts at each step, and `etl.py` asserts that the revenue waterfall reconciles.

## 8. Limitations and future work

* Data is synthetic. Real data would need column mapping and a larger volume plan (chunked loads, indexed joins).
* COGS treatment on returns (restocked or not) and allocation of delivery costs by value share are assumptions; return condition data would improve them.
* Cost estimation for missing costs uses a category median ratio, so those rows are flagged rather than trusted.
* Possible extensions: incremental loads, automated tests for each rule, cohort and lifetime-value analysis, and scheduled refresh.
