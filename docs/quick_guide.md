# Quick guide

The everyday commands, in order. Run them from the project folder with the environment active:

```bash
cd ~/DataGen
source .venv/bin/activate
```

Every command below was run end to end on a test company before this guide was written.

---

## Start from scratch: delete everything

Run this once to remove every company you have now.

```bash
scmgen workspace delete --all --purge
```

- It lists the companies it will delete and asks you to type `all`.
- For each company it drops both MySQL databases (`scm_<code>_oltp` and `scm_<code>_sim`) and its DuckDB files (`data/duckdb/<code>.duckdb` and `<code>_sim.duckdb`).
- `--purge` also removes the company from the registry, so you can reuse its code.

To check that nothing is left, run `scmgen workspace list`. It should show no rows.

Not deleted: MySQL itself, the `scm_app` user and the `scm_control` registry, which you keep. Old validation reports in `data/reports/` also stay. You can delete that folder by hand if you want: `rm -rf data/reports/*`.

---

## 1. Create a new company with the industries you choose

```bash
scmgen workspace create --code acme --company "Acme Home Traders Pvt. Ltd." --industries home_kitchen,furniture --seed 42
```

| Option | What it means |
|---|---|
| `--code` | Short name you use in every later command: 2–10 lowercase letters or digits (`acme`, `shop1`) |
| `--company` | Any fictional company name |
| `--industries` | One or more, separated by commas: `beauty`, `electronics`, `fashion`, `fmcg`, `furniture`, `home_kitchen`, `industrial_mro`, `office_stationery` |
| `--seed` | Any number. The same seed always gives exactly the same data. If you leave it out, a random one is chosen and recorded |

This creates the empty company databases. No data yet.

---

## 2. Generate data: first the history, then new days

### First time: generate the history

```bash
scmgen generate history -w acme --preset small
```

- **What it creates:** master data first (products, customers, suppliers, warehouses, employees, opening stock). Then it simulates every business day: orders, purchases, shipments, invoices, payments, returns, expenses, GST and accounting.
- **How much:** the `small` preset gives 3 months of history ending yesterday, in a few minutes. `medium` gives 24 months in about 18 minutes; `large` gives 36 months.
- **Choosing the dates yourself** (optional):
  ```bash
  scmgen generate history -w acme --preset small --set history.start_date=2026-01-01 --set history.end_date=2026-06-30
  ```
- **When it ends:** it prints `Validation PASSED: ... 0 failed`. The company is then `READY`.

### Later: add new days on top

```bash
scmgen generate continue -w acme --days 7
```

- It simulates the next 7 days after the last generated day, using the same settings. Existing rows aren't regenerated; new orders arrive and open orders move on (shipped, delivered, paid…).
- Leave out `--days` to fill every day up to yesterday.
- `scmgen generate plan -w acme` shows which days Continue would add, without writing anything.

---

## 3. Load into DuckDB, read it, then load only the new rows

### Load (first time = full copy)

```bash
scmgen load -w acme
```

This creates `data/duckdb/acme.duckdb`. It ends with `Load SUCCESS; reconciliation PASSED`, which means the row counts and totals in DuckDB match MySQL. All tables are in the `raw` schema (`raw.orders`, `raw.customers`…).

### Read it

**DuckDB command line** (installed):

```bash
duckdb -readonly data/duckdb/acme.duckdb
```
```sql
SHOW ALL TABLES;
SELECT COUNT(*) AS orders, MAX(order_date) AS last_day FROM raw.orders;
SELECT order_status, COUNT(*) FROM raw.orders GROUP BY 1 ORDER BY 2 DESC;
.quit
```

**Python** (Jupyter, VS Code, a script):

```python
import duckdb
con = duckdb.connect("data/duckdb/acme.duckdb", read_only=True)
con.sql("SELECT order_status, COUNT(*) AS n FROM raw.orders GROUP BY 1 ORDER BY 2 DESC").show()
df = con.sql("SELECT * FROM raw.customers").df()    # as a pandas DataFrame
```

Open it with `read_only=True` (or `-readonly`). DuckDB allows only one writer at a time, so a read-only connection doesn't block the next load. Close your connection before running `scmgen load`.

### New rows → incremental load

```bash
scmgen generate continue -w acme --days 5     # new business days in MySQL
scmgen load -w acme                            # copies only what changed since the last load
```

The second command prints `INCREMENTAL_APPEND` and `INCREMENTAL_MERGE` lines instead of `FULL`:

- **APPEND:** new rows only, for tables whose rows never change (ledgers, events).
- **MERGE:** new rows are inserted and changed rows are updated, e.g. an order that went from SHIPPED to DELIVERED.

Run `scmgen load -w acme` again right away and it reads 0 rows: nothing is copied twice.

To see what each load did:

```bash
scmgen load status -w acme
```
```sql
-- in duckdb -readonly data/duckdb/acme.duckdb
SELECT load_type, rows_extracted, rows_inserted, rows_updated, started_at
FROM etl_meta.etl_load_history WHERE source_table = 'orders' ORDER BY started_at;
```

Example from the test run (2 months generated, then 5 days added):

| load_type | rows_extracted | rows_inserted | rows_updated |
|---|---|---|---|
| FULL | 1180 | 1180 | 0 |
| INCREMENTAL_MERGE | 302 | 100 | 202 |
| INCREMENTAL_MERGE | 0 | 0 | 0 |

The second load brought in 100 new orders and 202 orders whose status changed. The third load had nothing new.

If you ever want a clean full copy again: `scmgen load -w acme --full`.

---

## 4. Delete one company

```bash
scmgen workspace delete -w acme --purge
```

It asks you to type `acme`, then removes that company completely:

- its two MySQL databases (`scm_acme_oltp`, `scm_acme_sim`)
- its DuckDB files (`data/duckdb/acme.duckdb`, `acme_sim.duckdb`)
- its registry entry (with `--purge`)

After that it no longer exists and the code `acme` can be used again. Without `--purge`, the data is still deleted, but the company stays in `workspace list` as `ARCHIVED`.

To delete every company at once, see [Start from scratch](#start-from-scratch-delete-everything).

You can also delete from the control panel (`scmgen ui` → *Reset* page): type the code, tick *remove the registry entry*, then click *Delete workspace*.

**If it says "busy":** a generation run for that company is still going. Wait for it to finish, or cancel it in the control panel's *Run monitor*, then delete again.

---

## The whole flow in one place

```bash
scmgen workspace delete --all --purge                       # clean slate (type: all)
scmgen workspace create --code acme --company "Acme Home Traders Pvt. Ltd." --industries home_kitchen,furniture --seed 42
scmgen generate history -w acme --preset small              # 3 months of history
scmgen load -w acme                                         # full load to data/duckdb/acme.duckdb
duckdb -readonly data/duckdb/acme.duckdb -c "SELECT COUNT(*) FROM raw.orders"
scmgen generate continue -w acme --days 5                   # 5 more days
scmgen load -w acme                                         # incremental load
duckdb -readonly data/duckdb/acme.duckdb -c "SELECT COUNT(*) FROM raw.orders"   # higher count
scmgen workspace delete -w acme --purge                     # remove it (type: acme)
```

The control panel (`scmgen ui`, http://localhost:8501) runs the same commands with buttons. The same steps in the browser, with every feature explained, including CDC and anomalies: [`ui_guide.md`](ui_guide.md).
