# Control panel guide

How to test every feature of the app from an empty start, using only the browser. Each part explains **what** the thing is, **why** it exists, **how** to do it and **what you should see**.

The numbers in "what you should see" come from a test run of exactly these steps: a 6-month Small company, home_kitchen + furniture, seed 42. Your numbers will be close, but not identical if you choose other industries or dates.

Total time: about 45 minutes.

---

## 0. Open the app

The terminal is needed once, only to start the app:

```bash
cd ~/DataGen
source .venv/bin/activate
scmgen ui
```

Open **http://localhost:8501** in your browser. Leave the terminal open while you use the app; closing it stops the app.

### How the screen is laid out

- **Left sidebar:**
  - **Workspace** dropdown: the company you're working on.
  - Under it: that company's name, status and the last day its data covers.
  - **Refresh** button.
  - **Page** list: Workspaces, Run monitor, Data explorer, Validation, Scenarios, DuckDB load, Simulation, Table guide, Reset.
- **Main area:** the page you picked.

### How buttons work

Every action runs in the background. When you click a button:

1. A green bar appears: *Started in the background: … (job …)*.
2. A **Background jobs** box at the bottom of the page shows the job's latest output.
3. The page doesn't update by itself (except *Run monitor*). Click **Refresh** in the sidebar to see the job's progress and result.
4. The job's title ends with one of:
   - **finished (exit 0):** it worked.
   - **finished (exit 1):** it failed. Open the box to read why.

You can close the browser while a job runs. The job keeps going, and you see its result when you come back.

---

## 1. Workspaces: create your company

### What is a workspace?

A workspace is **one fictional company with all of its data**, kept completely separate from every other company. Creating one gives the company four storage places:

| Name | What's in it |
|---|---|
| `scm_<code>_oltp` (MySQL) | The **clean** operational database: the company's "live system", 92 tables |
| `scm_<code>_sim` (MySQL) | A **sandbox** copy where we deliberately break data (part 8) |
| `data/duckdb/<code>.duckdb` | The **analytics copy** of the clean data (part 5) |
| `data/duckdb/<code>_sim.duckdb` | The analytics copy of the sandbox |

### Why have workspaces?

So you can keep several companies side by side, for example an electronics trader and a beauty retailer. You can also try things on one company without risk to the others, and delete one without touching the rest. A real data team works the same way, with separate environments (dev, test, prod) that never mix.

### How to create one

1. Page **Workspaces**. With no companies yet, the **Create a workspace** form is already open.
2. Fill in the form:

   | Field | What to enter | Why |
   |---|---|---|
   | **Code** | A short name, 2–10 lowercase letters or digits, e.g. `shop1` | It names the databases and files (`scm_shop1_oltp`, `shop1.duckdb`) |
   | **Company name** | Any fictional name | Printed on invoices, orders and the company profile |
   | **Industries** | One or more, e.g. `home_kitchen` + `furniture` | Decides what the company sells (see the table below) |
   | **Random seed** | Keep `42` | See the explanation below |

3. Click **Create**. It takes about 3 seconds.
4. Click **Refresh** in the sidebar. Your company now appears in the **Workspace** dropdown, with status `READY`.

### Industries

Each industry is an **industry pack**: a ready-made description of a type of business. The pack sets:

- the product catalogue
- price ranges
- GST rates (e.g. 18% on electronics, 5% on many food items)
- seasonality (e.g. sales peaks at Diwali)
- the split between business customers (B2B) and consumers (B2C)

The available packs are beauty, electronics, fashion, fmcg (fast-moving consumer goods), furniture, home_kitchen, industrial_mro (maintenance, repair and operations supplies) and office_stationery. Choosing two or more gives a company with several business units.

### The seed

All the "random" choices in the data come from this number: which customer orders, how many, which payment fails. The same seed always gives exactly the same data. That matters when you test a data pipeline: you can regenerate the same data and compare results. A different seed gives a different but equally realistic company.

**What you should see:** in the workspace table, your code, company, industries, status `READY` and seed `42`. *committed through* is empty because there's no data yet.

---

## 2. Workspaces: generate the history

### What is generating history?

The company exists but has no data yet. **Generate history** simulates the company's past, one business day at a time.

1. **Master data** first: products, suppliers, customers, warehouses, employees, bank accounts, chart of accounts, opening stock.
2. **Then every day, in order:**
   - customers place orders, and the warehouse picks, packs and ships
   - carriers deliver, invoices are issued and customers pay (some late, some failed)
   - goods are returned, stock runs low and purchase orders go to suppliers
   - suppliers deliver and are paid, expenses are booked and GST returns are filed
   - every money movement is posted to the accounts

Everything happens *because of* earlier events. An invoice exists because a shipment left; a payment because an invoice was due. This is why the tables join up like a real company's database, instead of being random rows.

### Why generate from the past?

Analytics needs history: trends, month-on-month comparisons, seasonality. Starting in the past also leaves room to add "new" days later (part 6), the way a live system keeps receiving new data.

### How to generate

1. In the sidebar, pick your company.
2. On page **Workspaces**, under **Generate — shop1**, left column **Historical generation**, set these fields:

   | Field | Set it to | Why |
   |---|---|---|
   | **Volume preset** | `small` | How busy the company is. `small` takes minutes. `medium` (about 18 minutes for 2 years) and `large` are for later, bigger tests |
   | **Start date** | `2026-04-01` | The default is one year back. 6 months is enough to test and twice as fast |
   | **End date** | `2026-09-30` | **Must be in the past.** Ending about 6 weeks before today leaves days for *Continue* in part 6 |
   | **Scenario profile** | `default` | `stress` simulates a rough year: more cancellations, failed payments, returns and late suppliers. Try it on a second company later |

3. Click **Generate history**.
4. Go to page **Run monitor** to watch it.

### Run monitor: watching a run

This page refreshes by itself every 5 seconds. It shows:

- **Latest run:** run number, status (`RUNNING` → `COMPLETED`) and days done, e.g. *183 / 183 days*, with a progress bar.
- **Step table:** what the run is doing (master data, daily simulation, validation), with rows generated and rows per second.
- **Cancel after the current day:** stops a run cleanly. The day in progress either completes or is rolled back completely, so the data is never left half-written. You can carry on later with *Continue*.
- **Runs:** every run of this company: the first history, later continues and rebuilds.

**What you should see:**

- After about **2 minutes**: *Latest run … COMPLETED, 183 / 183 days*.
- In the sidebar (after **Refresh**): *committed through: 2026-09-30*.
- The job box on *Workspaces* ends with *Validation PASSED: 99 passed, 0 failed*. Every run checks its own data at the end (part 4).

---

## 3. Data explorer: look at the data

### What it is

A quick look at the clean MySQL database, without writing SQL.

### How to use it

| Tab | What to do | What it shows you |
|---|---|---|
| **Row counts** | Click **Count rows (exact)** | All 92 tables with their row counts. `journal_entry_lines`, `inventory_transactions` and `tax_transactions` are the biggest: they're the ledgers (records of every money, stock and tax movement) |
| **Sample rows** | Pick a table, e.g. `orders`, `invoices`, `customer_payments` | The newest 200 rows. Look at the order numbers (`SO/KA/2026-27/002087`), the statuses and the amounts |
| **Charts** | Just open it | Orders per month by sales channel, revenue per month by business unit, stock value per warehouse |

**What to look for:** realism. Different channels have different volumes, there are festive peaks, and each business unit has its own revenue line. Each warehouse holds a different amount of stock.

---

## 4. Validation and Scenarios: is the data correct?

### What validation is

There are **101 automatic checks** (the "V-" rules from the design documents) that prove the data is consistent. Some examples:

- every accounting entry balances (debits = credits)
- stock never goes negative
- stock balances match the stock movements exactly
- an invoice total equals its lines plus GST
- nobody returns more than was delivered

### Why it matters

Generated data is only useful if it behaves like a real company's data. If the data itself were wrong, your later SQL and dbt models would "find" problems that don't exist. Every run validates itself at the end. A company whose validation fails is marked `NEEDS_ATTENTION`, and *Continue* and *loading* are then blocked, so bad data never travels further.

### How to use it

1. Page **Validation**. The latest report is already shown, from the end of the generation run.
2. Click **Run all validation checks** to run them again now. Click **Refresh**, then pick the newest report in the **Report** dropdown.

**What you should see:**

- **Passed 99–100, Failed 0, Error 0.**
- One or two checks are *Not applicable*: they only apply to events that haven't happened yet in a short history.
- The table lists every rule with a plain-English description.
- If a check ever fails, a box opens for it with sample rows that break the rule.

### Scenarios page

#### What it is

The 20 business situations the requirements asked for, each with its **measured share** in your data and the **expected band** (range). For example:

- successful deliveries: 94%, band 85–97%
- partial deliveries: 15%, band 5–25%
- cancelled orders: 3%, band 3–10%
- also failed payments, overdue invoices, rejected supplier goods, stock shortages and late supplier deliveries

#### Why it matters

It proves the company isn't "too perfect". Real data has problems, and analytics practice needs them: cancellations, late payments, returns.

**What you should see:** 20 rows, status **OK** for each (inside its band). The *settings* column names what to change if one were outside its band.

---

## 5. DuckDB load: copy the data for analytics

### What DuckDB is, and why copy at all

MySQL is the company's **operational** database. It's built for many small writes: one order, one payment. **DuckDB** is an **analytical** database in a single file. It's built for big reads, like "revenue per month per region over two years".

Real companies never run heavy analytics on the live system. They copy the data into an analytics store, a warehouse or lakehouse. This page is that copy step: it's called **ETL** (extract, transform, load).

### How to load

1. Page **DuckDB load**. It says *No DuckDB replica yet*.
2. Click **Incremental load**. The first load is always a full copy. Then click **Refresh**.

**What you should see:**

- **Background job output:**
  - *Full load: first load*
  - *FULL  92 tables  218,521 rows read  218,521 inserted*
  - *Load SUCCESS; reconciliation PASSED*
  - It takes about 6 seconds.
- **Above the table:** *Replica data/duckdb/shop1.duckdb — source build matches — high-water mark 2026-09-30 23:59:59*.
- **The table:** one row per table, with its strategy (FULL / APPEND / MERGE), watermark, rows, last load and *reconciliation PASSED*.

**Reconciliation** means that after every load, the app compares DuckDB with MySQL: row counts and money totals per table. *PASSED* means the copy is exact.

### Read the DuckDB file: "Query the DuckDB replica"

At the bottom of the same page:

1. **Replica:** choose **Clean replica (shop1.duckdb)**.
2. Tick **List the replica's tables**. The tables are in schema `raw` (copied data) and `etl_meta` (load bookkeeping).
3. The SQL box has an example. Click **Run on DuckDB**:

   ```sql
   SELECT order_status, COUNT(*) AS orders FROM raw.orders GROUP BY 1 ORDER BY 2 DESC
   ```

   You get counts per status: CLOSED 2,054, DELIVERED 316, CANCELLED 77, SHIPPED 54, …
4. Try your own queries, for example revenue by month:

   ```sql
   SELECT strftime(invoice_date, '%Y-%m') AS month, SUM(total_amount) AS invoiced
   FROM raw.invoices WHERE invoice_status = 'ISSUED' GROUP BY 1 ORDER BY 1
   ```

The replica opens **read-only**, so nothing you type can change it. A `DELETE` is refused with an error. If a load is running at that moment, the page says the replica is busy; try again a few seconds later.

---

## 6. Continue + incremental load: new data arrives

### What Continue is

Continue simulates **more days** after the last one, with the same settings. Nothing old is regenerated. New orders arrive, and *existing* orders move on: SHIPPED becomes DELIVERED, invoices get paid, returns come back.

### Why it matters

This is how a live system behaves, and it's the core problem of data engineering: **how do you copy only what changed since last time**, instead of copying everything every night?

### How to do it

1. Page **Workspaces**, right column **Continue**. Set **Days** to `7`.

   The grey **Plan** line tells you what will happen before you click: *Plan: 7 day(s) 2026-10-01 … 2026-10-07*. With **0** days it fills every day up to yesterday.
2. Click **Continue**. It takes about 10 seconds. Then click **Refresh**: *committed through* is now *2026-10-07*.
3. Page **DuckDB load** → **Incremental load** → **Refresh**.

**What you should see:**

```
FULL                 24 tables       1,437 rows read       1,437 inserted          0 updated
INCREMENTAL_APPEND   21 tables       7,497 rows read       7,497 inserted          0 updated
INCREMENTAL_MERGE    47 tables       5,065 rows read       3,422 inserted      1,643 updated
Load SUCCESS; reconciliation PASSED
```

There are three ways a table gets loaded:

| Strategy | Used for | What happens |
|---|---|---|
| **FULL** | Small reference tables (states, GST rates, channels…) | Copied completely every time: cheaper than tracking changes |
| **APPEND** | Tables whose rows never change once written (ledgers, events) | Only new rows are added |
| **MERGE** | Tables whose rows change (orders, invoices, payments…) | New rows are inserted; changed rows are updated in place |

**How the app knows what changed:** every row has an `updated_at` time. The loader remembers how far it got for each table: its **watermark**, shown in the table on this page. Next time it reads only rows newer than that point.

Now see it for one table in the query box (clean replica):

```sql
SELECT load_type, rows_extracted, rows_inserted, rows_updated, started_at
FROM etl_meta.etl_load_history WHERE source_table = 'orders' ORDER BY started_at
```

| load_type | rows_extracted | rows_inserted | rows_updated |
|---|---|---|---|
| FULL | 2521 | 2521 | 0 |
| INCREMENTAL_MERGE | 343 | 177 | 166 |

So 177 orders were placed in the 7 new days, and 166 older orders changed status.

Click **Incremental load** once more. Every incremental line now shows **0 rows read**: a load you repeat never copies anything twice. This property is called **idempotent**.

The other two buttons on the page:

- **Full reload:** copies everything again from scratch.
- **Reload one table:** type a table name, e.g. `orders`, to copy just that table again.

---

## 7. Table guide: understand each table

### What it is

A built-in manual for all 92 tables, so you can write SQL without opening the design documents.

### How to use it

1. Page **Table guide**.
2. Pick a **Module** (e.g. *Sales & Order Tables*), or keep *(all)*.
3. Pick a **Table**, e.g. `orders`. It shows a description and **Rows in this workspace**.
4. Look through the tabs:

   | Tab | What it shows |
   |---|---|
   | **Columns** | Every column with its type and meaning |
   | **Relations** | A diagram: which tables `orders` points to (customer, channel, warehouse…), and which tables point to it (order lines, shipments, invoices…) |
   | **How it is populated** | Which business event creates or changes these rows |
   | **Example queries** | Ready SQL. Click **Run on MySQL** to run it on your company's data |

**Tables worth reading:**

- `orders`: the centre of sales.
- `journal_entry_lines`: every money movement. All accounting reports are built from it.
- `inventory_transactions`: every stock movement. Stock balances are built from it.

---

## 8. Simulation: CDC and broken data

So far everything has been **clean**. Real pipelines also have to cope with bad data. This page lets you break data on purpose and check that the tests catch it.

Your clean company is never touched. Everything here happens in the sandbox (`scm_shop1_sim`) and in `shop1_sim.duckdb`. The page's user account can only *read* the clean database.

### 8a. Prepare: four buttons under "1. Prepare"

Click them in this order. After each one, click **Refresh** and wait until its job says *finished (exit 0)*.

| Button | What it does | Why | What you should see |
|---|---|---|---|
| **Reset staging copies** | Copies all 92 clean tables into the sandbox, **without** the database rules (keys, checks) that normally block bad data | Like a raw landing zone in a real pipeline: data arrives without guarantees | *92 tables, 229,541 rows*; the **Staging copies** tile shows 92 |
| **Load sim replica** | Loads the sandbox into `shop1_sim.duckdb`, with the same loader as part 5 | The broken data must travel through a real pipeline | *FULL 92 tables … reconciliation PASSED* |
| **CDC capture** | Records **change events**: one event per row that was created or changed. The first capture is a **SNAPSHOT** of every row | See "What CDC is" below | Lines like *orders SNAPSHOT …*; **Change events** tile ≈ 78,000 |
| **CDC apply** | A **consumer** reads the events in order and builds two copies of each table | See below | *78,205 events … rejected none*; *Convergence: 47 tables match the source* |

#### What CDC is

CDC stands for **change data capture**. Instead of asking "which rows are newer than my watermark?" (part 6), the source publishes a **stream of events**: "order 2087 was inserted", "order 2087 changed from DELIVERED to CLOSED". Tools like Debezium and Kafka work this way.

The consumer turns the stream into two tables in `shop1_sim.duckdb`:

- `cdc_current.orders`: the latest state of each row.
- `cdc_history.orders`: **every version** of each row, with `_valid_from`, `_valid_to` and `_is_current`. This is called **SCD Type 2** (slowly changing dimension, type 2). It lets you answer "what was this order's status on 30 September?"

#### See the history grow

1. Page **Workspaces** → **Continue** with **Days** = `2`.
2. Back on **Simulation**: **CDC capture** (now *INSERT* and *UPDATE* events instead of SNAPSHOT), then **CDC apply**.
3. Page **DuckDB load** → query box → **Replica: Simulation replica (shop1_sim.duckdb)**. Run:

   ```sql
   SELECT order_number, order_status, _operation, _valid_from, _valid_to, _is_current
   FROM cdc_history.orders
   WHERE order_id IN (SELECT order_id FROM cdc_history.orders GROUP BY 1 HAVING COUNT(*) > 1 LIMIT 3)
   ORDER BY order_id, _lsn
   ```

   You see the same order twice:
   - **SNAPSHOT:** DELIVERED, valid until 2026-10-08, `_is_current` false.
   - **UPDATE:** CLOSED, valid from 2026-10-08, `_is_current` true.

### 8b. Inject anomalies: "2. Inject anomalies"

An **anomaly** is a deliberate data problem. Pick a **Scenario**; the table below the picker shows exactly what it will break and which checks **must fail** because of it. Then click **Inject**.

| Scenario | What it breaks | Which tests should catch it |
|---|---|---|
| `data_quality` | Duplicate customers, orders missing their customer, order lines pointing to products that don't exist, impossible timestamps, invoices dated in the future | Generic data-quality tests: not-null, unique, relationships, timestamps (the same kinds of tests as dbt's) |
| `business_rules` | Wrong amounts and quantities that *look* valid: a stock balance that drifts, delivery scans in the wrong order | Only business-rule and reconciliation checks can see these |
| `pipeline` | Late-arriving rows, hard-deleted payments, a changed table structure (schema drift) | The loader of the sim replica (reconciliation, drift detection) |
| `cdc_stream` | Duplicate events and **stale** (out-of-order) events in the change stream | The CDC consumer, which must reject them |

Inject all four, one after another. After each one, click **Refresh**: the **Registered anomalies** tile grows, and a table lists every anomaly. *pipeline* needs a sim load first, and *cdc_stream* needs a CDC capture first; you did both in 8a.

**Seed:** the same seed breaks the same rows again. A different seed picks different rows.

### 8c. Detection report: "3. Detection report"

#### What it is

One button that runs the whole set of checks over the broken sandbox:

1. a fresh sim load
2. the CDC consumer
3. all validation checks
4. the generic data-quality checks

Then it compares what failed against what was injected.

#### Why it matters

It answers the question every data team must answer: **do our tests catch the problems that really happen?**

Click **Run detection report**, then **Refresh**.

**What you should see:**

- **Caught 39, Partly caught 0, Missed 0, Unexplained failures 0.**
- A table with one row per anomaly. Each expected check has ✓ (it failed, as it should) or ✗ (it didn't).

| Result | Meaning |
|---|---|
| **Caught** | Every check that should fail did fail |
| **Partly caught** | Only some of them failed |
| **Missed** | None failed: a gap in the tests |
| **Unexplained failure** | A check failed that no anomaly explains: a false alarm, or a real bug |

To start the sandbox over, click **Reset staging copies** again. It throws away every anomaly and copies the clean data afresh.

---

## 9. Rebuild: same seed, same data

### What it is

Rebuild drops the company's data and regenerates the same history with the same seed and settings.

### Why have it

To prove the data is **reproducible**. A pipeline test you run today and next month gets the same input. It's also useful after you've experimented and want the original company back.

### How

1. Page **Workspaces**, right column, **Rebuild**.
2. Type the code (`shop1`) in the box, then click **Rebuild**. It takes about 3 minutes for 6 months.

**What you should see:**

- The job ends with *Validation PASSED*.
- *Run monitor* shows a new run.
- On **DuckDB load**, the caption says *source build CHANGED: next load is full*. The replica notices that the source was rebuilt, so the next **Incremental load** copies everything again.

---

## 10. Reset: delete a company

### What it does

Deletes the company completely: both MySQL databases, both DuckDB files and its reports.

### Why

To clean up after testing, or to start over.

### How

1. Pick the company in the sidebar.
2. Page **Reset**.
3. Tick **Also remove the registry entry (the code can be reused)**.
4. Type the code in the box, then click **Delete workspace**.
5. Click **Refresh**. The company is gone from the dropdown.

If you **don't** tick the box, the data is still deleted, but the company stays in the workspace list as `ARCHIVED`, as a record that it existed. Its code can't be used again until you delete it once more with the box ticked.

---

## Checklist

| # | Feature | Page | Done when you see |
|---|---|---|---|
| 1 | Create a company | Workspaces | Status `READY` in the table |
| 2 | Generate 6 months | Workspaces → Run monitor | *COMPLETED 183 / 183 days*, *Validation PASSED* |
| 3 | Explore data | Data explorer | Row counts, sample rows, 3 charts |
| 4 | Validation | Validation | Failed 0 |
| 5 | Scenarios | Scenarios | 20 × OK |
| 6 | First load | DuckDB load | *FULL 92 tables*, *reconciliation PASSED* |
| 7 | Query DuckDB | DuckDB load → query box | Rows returned |
| 8 | Continue 7 days | Workspaces | *committed through* moves forward |
| 9 | Incremental load | DuckDB load | INCREMENTAL lines; a repeat load reads 0 rows |
| 10 | Table guide | Table guide | Columns, diagram, example query runs |
| 11 | CDC | Simulation | *Convergence: 47 tables match*; SCD2 versions in `cdc_history` |
| 12 | Anomalies | Simulation | Caught = all, Missed 0, Unexplained 0 |
| 13 | Rebuild | Workspaces | Same data again; next load is full |
| 14 | Delete | Reset | Company gone |

## If something goes wrong

- **A button seems to do nothing:** click **Refresh**. Jobs run in the background, and the page only updates when refreshed.
- **A job shows exit 1:** open its box. The last lines say what went wrong. Send them to me.
- **"busy":** another job for the same company is still running. Wait for it to finish (Run monitor), then try again.
- **Continue says 0 days:** your history already reaches yesterday. Next time, end the history further in the past, or just wait a day.
- **`NEEDS_ATTENTION` status:** validation failed after a run. Open *Validation* to see which rule failed, and send me the report.
