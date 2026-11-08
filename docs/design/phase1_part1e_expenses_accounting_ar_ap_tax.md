# Phase 1 – Part 1E: Expenses, Accounting, AR/AP and Tax

**Project:** Synthetic Supply Chain OLTP Database & Data Engineering Practice Platform
**Status:** Accepted (D9). Baseline for Phase 2
**Last updated:** 2026-11-08
**Builds on:** Parts 1A–1D (D1–D48). Accounting master data (COA, system roles, periods, cost centres, tax accounts) is in 1B; document conventions in 1C §2

---

## 0. Scope

This part defines **16 tables and 1 view**, and the **double-entry posting rule for every business event** in Parts 1C and 1D. It resolves Q2 (for journals) and Q3.

| Group | Tables | Count |
|---|---|---|
| Expenses | `expenses`, `expense_line_items`, `expense_payments`, `expense_attachments_metadata` | 4 |
| Journals & ledger | `journal_entries`, `journal_entry_lines`, `gl_period_balances`, view `v_general_ledger` | 3 + view |
| Bank | `bank_transactions`, `bank_reconciliations` | 2 |
| Receivables | `accounts_receivable`, `customer_payment_allocations` | 2 |
| Payables | `accounts_payable`, `supplier_payment_allocations` | 2 |
| Tax | `tax_transactions`, `gst_returns`, `gst_return_line_items` | 3 |

That brings the designed total to **89 of the 92 tables**. The remaining three are system tables (`document_sequences`, `generation_runs`, `generation_run_steps`), designed in 1G with the generation architecture.

**The accounting idea in one paragraph.** Every financially relevant business event produces exactly one **journal entry** in the same database transaction as the event. That's the only way anything gets into the books (master prompt §4I: accounting must be a *consequence* of business events). Journal lines are the single source of truth. The general ledger is a view over them, and period balances are a rebuildable summary. The AR and AP subledgers are open-item lists whose totals must equal their GL control accounts. GST is recorded per document line in `tax_transactions` and summarised into monthly returns, whose settlement is itself a journal entry.

---

## 1. Decisions Made in This Part

| # | Decision | Choice | Why |
|---|---|---|---|
| D49 | Open-item subledgers (**refines 1A §5 J**) | `accounts_receivable` holds one open item per invoice, credit note, successful customer payment **and refund**. `accounts_payable` holds one per supplier invoice, expense **and supplier payment**. Payments start as unapplied credit items and are **cleared** against invoices by allocations. Allocations **don't post journals**: both sides already sit in the same control account | This is how open-item accounting works in real ERPs (SAP "clearing"). Prepayments, advances, partial payments and credit notes all fit one model, and subledger total = control account at every moment. 1A described AR/AP as one row per invoice/credit note only, which couldn't represent a B2C prepayment that arrives before its invoice exists |
| D50 | Journal source linking (**Q2 resolved**) | Same pattern as D28: `journal_entries` has one nullable FK per source document type (16), a readable `journal_source` discriminator, a CHECK that the FK set matches the source, and a **generated `posting_key` with a UNIQUE index**, so an event can never be posted twice | Real FKs for lineage. The posting key makes generation **idempotent and restartable** (master prompt §14): a re-run that tries to post the same event fails at the database instead of creating a duplicate |
| D51 | AP open items (**Q3 resolved**) | `accounts_payable` has exactly one of `supplier_invoice_id` / `expense_id` / `supplier_payment_id` (CHECK), and exactly one payee: `supplier_id` or `employee_id` (employee expense claims) | Same pattern everywhere, enforced by the database |
| D52 | COGS timing | Cost of goods sold is posted **inside the sales invoice journal** (dispatch / handover), using the issue values from `inventory_transactions` | Invoice, stock issue and COGS are one business event (D4). One journal per event keeps revenue and its cost in the same entry, and a cancellation (D40) reverses both at once |
| D53 | Receipts with delayed settlement | UPI, card, net-banking and COD receipts post to **Payment Clearing**. The bank settlement (a daily gateway batch or weekly COD remittance) posts from the `bank_transactions` row: Dr Bank (net), Dr Gateway Charges, Cr Clearing. NEFT/RTGS/IMPS/cash post straight to Bank. Cheques post only when cleared | Matches how money actually arrives. It creates realistic clearing balances and bank-reconciliation work, and books gateway fees when they're charged |
| D54 | Year-end | **No closing journal.** P&L accounts restart at zero each FY inside `gl_period_balances`; Retained Earnings' opening balance absorbs prior years' profit | Most ERPs report this way. Closing entries would make every P&L report filter them out |
| D55 | Inter-GSTIN transfer tax | One journal at dispatch: Dr Input IGST (destination GSTIN), Cr Output IGST (source GSTIN), alongside the stock movement. No inter-branch accounts | The company keeps one set of books. Branch accounts would always net to zero and add nothing for analytics; the tax still appears correctly in each GSTIN's returns |
| D56 | Payroll | One `PAYROLL` expense per month (lines per cost centre). Posting: Dr Salaries, Cr Salaries Payable. Paid on the 1st working day of the next month. Gross = net in v1. **Deferred R27:** PF/ESI/TDS/professional-tax deductions | Salaries are the largest expense and must exist (master prompt §4H), but statutory payroll is a module of its own |
| D57 | Expenses through AP | Every non-payroll expense (vendor bill, employee claim, petty cash) creates an AP open item; payments clear it. Petty cash bills are paid the same day from the warehouse's petty-cash account | One uniform flow. Petty cash still shows up as cash spent the same day |
| D58 | GST returns | Per GSTIN per month: **GSTR-1** (outward supplies detail, due 11th) and **GSTR-3B** (summary + payment, due 20th). On 3B filing: Dr Output GST (period liability), Cr Input GST (credit used, in the legal set-off order), Cr Bank (cash). Unused credit stays in the Input GST accounts and carries forward | Reproduces the real monthly cycle. The input/output accounts reconcile exactly to the returns, which is a very good reconciliation exercise |
| D59 | Posting date | `posting_date` = the event date if its period is OPEN when the event is recorded, otherwise the first day of the earliest open period. `document_date` always keeps the business date | Realistic late postings (e.g. a supplier invoice dated 28 Mar, received 10 Apr after March closed, posts in April) |
| D60 | Repair cost (**amends 1D**) | `defective_stock.repair_cost_amount` is a **memo** (internal labour estimate) and doesn't post. Repairs by outside vendors come through normal REPAIRS expenses | Avoids a second source for the same cost |
| D61 | Immutable journals | A POSTED journal is never edited or deleted. Corrections are a `REVERSAL` journal mirroring every line, followed by a new correct posting where needed. The original's status becomes `REVERSED` (the only update allowed) | Audit principle, and the master prompt asks for reversals and adjustments |
| D62 | Balance enforcement | The posting engine builds the whole entry in memory and refuses to insert it unless Σ debits = Σ credits. The header stores `total_debit` / `total_credit` with CHECK equal; a validation re-sums lines | MySQL can't check a cross-row sum inside a transaction without triggers. This combination makes an unbalanced journal impossible from the engine and detectable if anything else writes one |
| D63 | Settlement link (**amends 1D**) | `customer_payments` gets `settlement_bank_transaction_id` (FK → `bank_transactions`), linking each clearing-method payment to the bank settlement line that paid it out | Exact clearing-account reconciliation without fuzzy matching |

**Deferred (adds to 1B §1.2):** R27 payroll statutory deductions; R28 fixed-asset register (fixed assets exist only as opening balances, with monthly straight-line depreciation posted as a system journal).

**Open questions:** None new. Q4 and Q5 remain for 1G.

---

## 2. Expenses

#### `expenses`
An expense document: vendor bill, employee claim, petty-cash voucher or monthly payroll.

| Column | Type | NN | Notes |
|---|---|---|---|
| `expense_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `expense_number` | `VARCHAR(30)` | ✓ | UNIQUE (`EXP/<state>/<FY>/<SEQ>`) |
| `expense_type` | `VARCHAR(30)` | ✓ | `VENDOR_BILL` / `EMPLOYEE_CLAIM` / `PETTY_CASH` / `PAYROLL` |
| `supplier_id` | `BIGINT UNSIGNED` | | FK → `suppliers`. VENDOR_BILL, PETTY_CASH (optional shop) |
| `employee_id` | `BIGINT UNSIGNED` | | FK → `employees`. EMPLOYEE_CLAIM claimant |
| `vendor_bill_number` | `VARCHAR(40)` | | Supplier's invoice number ("supporting reference", master prompt §4H) |
| `expense_date` | `DATE` | ✓ | Bill / voucher date |
| `received_date` | `DATE` | ✓ | Date it reached Accounts |
| `due_date` | `DATE` | ✓ | From the supplier's terms; = `expense_date` for petty cash; month-end + 1 working day for payroll |
| `service_period_start` | `DATE` | | For period-based bills (rent, SaaS, payroll month) |
| `service_period_end` | `DATE` | | |
| `company_gst_registration_id` | `SMALLINT UNSIGNED` | | FK. Bill-to GSTIN where input credit is claimed; NULL when no GST |
| `warehouse_id` | `SMALLINT UNSIGNED` | | FK. Location the cost relates to (and the petty-cash box) |
| `is_inter_state` | `BOOLEAN` | ✓ | |
| `taxable_amount` … `total_amount` | `DECIMAL(18,2)` | ✓ | 1C §2.3 header pattern |
| `itc_eligible_tax_amount` | `DECIMAL(18,2)` | ✓ | Σ GST on lines whose category allows input credit; the rest is expensed |
| `recurrence_key` | `VARCHAR(40)` | | E.g. `RENT-DC-BLR`, `SAAS-CRM`. Groups recurring bills; NULL for one-offs |
| `expense_status` | `VARCHAR(30)` | ✓ | `DRAFT` / `SUBMITTED` / `APPROVED` / `REJECTED` / `CANCELLED` |
| `submitted_by_employee_id` | `BIGINT UNSIGNED` | ✓ | FK → `employees` |
| `approved_by_employee_id` | `BIGINT UNSIGNED` | | FK → `employees` (`approval_limit ≥ total`) |
| `approved_at` | `DATETIME(6)` | | Posting moment (journal + AP item) |
| `rejection_reason` | `VARCHAR(100)` | | |
| `description` | `VARCHAR(255)` | ✓ | |

- **Keys:** UNIQUE `(supplier_id, vendor_bill_number)` (duplicate-bill guard).
- **Constraints:**
  - CHECK lists.
  - Payee by type: `VENDOR_BILL` ⇒ `supplier_id IS NOT NULL AND employee_id IS NULL`; `EMPLOYEE_CLAIM` ⇒ `employee_id IS NOT NULL AND supplier_id IS NULL`; `PAYROLL` ⇒ both NULL; `PETTY_CASH` ⇒ `employee_id IS NULL AND warehouse_id IS NOT NULL`.
  - CHECK `vendor_bill_number IS NULL OR supplier_id IS NOT NULL`.
  - CHECK `(company_gst_registration_id IS NULL) = (cgst_amount + sgst_amount + igst_amount = 0)`.
  - CHECK `itc_eligible_tax_amount <= cgst_amount + sgst_amount + igst_amount`.
  - CHECK `(expense_status = 'APPROVED') = (approved_at IS NOT NULL)`; CHECK `(expense_status = 'REJECTED') = (rejection_reason IS NOT NULL)`.
  - CHECK `received_date >= expense_date AND due_date >= expense_date`; CHECK `service_period_end IS NULL OR service_period_end >= service_period_start`.
- **Indexes:** `(expense_date)`, `(supplier_id, expense_date)`, `(expense_status)`, `(recurrence_key, service_period_start)`.
- **Populated:**
  - **Recurring:** a schedule from config. Monthly rent per warehouse and HQ (landlord suppliers), electricity per site (varies with season, e.g. summer peaks), internet, SaaS and AI-tool subscriptions (fixed plus occasional plan changes), cloud hosting (grows with order volume), security and housekeeping, quarterly insurance and audit fees.
  - **Usage-driven:** monthly carrier freight bills (= Σ shipment freight cost, V-SH-05), marketplace commission bills (= Σ marketplace order value × commission %), campaign ad spend (≈ campaign budget, A1).
  - **Ad hoc (Poisson per month):** repairs, travel claims, stationery, professional fees.
  - **Petty cash:** 5–20 small vouchers per warehouse per month, each ≤ ₹10,000.
  - **Payroll:** month-end, one line per cost centre = Σ active employees' salaries, pro-rated for joiners and leavers.
  - About 3% of submitted expenses are rejected.

#### `expense_line_items`

| Column | Type | NN | Notes |
|---|---|---|---|
| `expense_line_item_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `expense_id` | `BIGINT UNSIGNED` | ✓ | FK → `expenses` |
| `line_number` | `SMALLINT UNSIGNED` | ✓ | |
| `expense_category_id` | `SMALLINT UNSIGNED` | ✓ | FK → `expense_categories` (gives the GL account) |
| `cost_center_id` | `SMALLINT UNSIGNED` | ✓ | FK → `cost_centers` |
| `marketing_campaign_id` | `BIGINT UNSIGNED` | | FK → `marketing_campaigns` (A1) |
| `description` | `VARCHAR(255)` | ✓ | |
| `sac_code` | `VARCHAR(8)` | | Services accounting code (GST) |
| `taxable_amount` | `DECIMAL(18,2)` | ✓ | |
| `gst_rate` | `DECIMAL(7,4)` | ✓ | 0 when outside GST |
| `cgst_amount`, `sgst_amount`, `igst_amount` | `DECIMAL(18,2)` | ✓ | 1C §2.3 |
| `is_itc_eligible` | `BOOLEAN` | ✓ | Snapshot of the category flag, and FALSE whenever the supplier is unregistered |
| `line_total` | `DECIMAL(18,2)` | ✓ | |

- **Keys:** UNIQUE `(expense_id, line_number)`.
- **Constraints:** 1C §2.3 amount CHECKs; CHECK `taxable_amount > 0`; CHECK `gst_rate > 0 OR NOT is_itc_eligible`.
- **Rules:** A campaign only on MARKETING-group categories, with the expense date inside the campaign window (± 15 days). PAYROLL lines only use the salaries category. Cost centre per category rule (warehouse costs → warehouse cost centre; HQ costs → department) (V-EX-02).

#### `expense_payments`
Payment(s) settling one expense. Payroll is usually one payment; partial payments are allowed.

| Column | Type | NN | Notes |
|---|---|---|---|
| `expense_payment_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `payment_number` | `VARCHAR(30)` | ✓ | UNIQUE (`EPAY/<state>/<FY>/<SEQ>`) |
| `expense_id` | `BIGINT UNSIGNED` | ✓ | FK → `expenses` |
| `payment_date` | `DATE` | ✓ | |
| `bank_account_id` | `SMALLINT UNSIGNED` | ✓ | FK → `bank_accounts` (PAYMENTS, PAYROLL or the warehouse's PETTY_CASH) |
| `payment_method_id` | `SMALLINT UNSIGNED` | ✓ | FK → `payment_methods` |
| `amount` | `DECIMAL(18,2)` | ✓ | |
| `bank_reference` | `VARCHAR(40)` | | |
| `payment_status` | `VARCHAR(30)` | ✓ | `INITIATED` / `COMPLETED` / `FAILED` / `REVERSED` |
| `failure_reason` | `VARCHAR(100)` | | |
| `processed_at` | `DATETIME(6)` | | |

- **Constraints:** CHECK `amount > 0`; CHECK lists; CHECK `(payment_status = 'FAILED') = (failure_reason IS NOT NULL)`; CHECK `payment_status = 'INITIATED' OR processed_at IS NOT NULL`.
- **Rules:** The expense is APPROVED. Σ COMPLETED payments ≤ expense total. Petty-cash expenses are paid only from the warehouse's petty-cash account, in cash ≤ ₹10,000. Payroll is paid from the PAYROLL account (V-EX-03). There's no allocation table: the FK to `expenses` *is* the allocation (1 expense : N payments).

#### `expense_attachments_metadata`
Metadata of supporting documents. No files are stored.

| Column | Type | NN | Notes |
|---|---|---|---|
| `expense_attachment_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `expense_id` | `BIGINT UNSIGNED` | ✓ | FK → `expenses` |
| `document_type` | `VARCHAR(30)` | ✓ | `VENDOR_INVOICE` / `RECEIPT` / `APPROVAL_EMAIL` / `CONTRACT` / `TRAVEL_TICKET` |
| `file_name` | `VARCHAR(200)` | ✓ | |
| `mime_type` | `VARCHAR(60)` | ✓ | `application/pdf`, `image/jpeg`, `image/png` |
| `file_size_bytes` | `INT UNSIGNED` | ✓ | |
| `storage_uri` | `VARCHAR(300)` | ✓ | Fictional (`s3://kaveri-docs.example/expenses/2026/04/…`) |
| `sha256_checksum` | `CHAR(64)` | ✓ | UNIQUE |
| `uploaded_at` | `DATETIME(6)` | ✓ | |
| `uploaded_by_employee_id` | `BIGINT UNSIGNED` | ✓ | FK → `employees` |

- **Constraints:** CHECK list; CHECK `file_size_bytes > 0`.
- **Populated:** ~95% of non-payroll expenses have 1–3 attachments, uploaded between `expense_date` and approval.

---

## 3. Journals and Ledger

#### `journal_entries`
Journal header: one per business event (D50) or system/manual entry.

| Column | Type | NN | Notes |
|---|---|---|---|
| `journal_entry_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `journal_number` | `VARCHAR(30)` | ✓ | UNIQUE (`JV/<FY>/<SEQ7>`) |
| `journal_source` | `VARCHAR(40)` | ✓ | Event type, §6 (e.g. `SALES_INVOICE`, `GOODS_RECEIPT`, `REVERSAL`) |
| `journal_type` | `VARCHAR(20)` | ✓ | `SYSTEM` (from an event) / `RECURRING` (depreciation) / `OPENING` / `MANUAL` / `REVERSAL` |
| `posting_date` | `DATE` | ✓ | D59 |
| `document_date` | `DATE` | ✓ | Business date of the source event |
| `accounting_period_id` | `SMALLINT UNSIGNED` | ✓ | FK → `accounting_periods` (must be OPEN when posted; contains `posting_date`) |
| `description` | `VARCHAR(255)` | ✓ | "Sales invoice KA2627-I000123" |
| `total_debit` | `DECIMAL(18,2)` | ✓ | Σ line debits |
| `total_credit` | `DECIMAL(18,2)` | ✓ | Σ line credits |
| `entry_status` | `VARCHAR(20)` | ✓ | `POSTED` / `REVERSED` |
| `reverses_journal_entry_id` | `BIGINT UNSIGNED` | | FK → self, UNIQUE. Set on REVERSAL entries |
| *Source FKs (all `BIGINT UNSIGNED`, nullable)* | | | `goods_receipt_id`, `supplier_invoice_id`, `supplier_payment_id`, `invoice_id`, `credit_note_id`, `customer_payment_id`, `refund_id`, `sales_return_id`, `shipment_id`, `stock_adjustment_id`, `stock_transfer_id`, `defective_stock_id`, `expense_id`, `expense_payment_id`, `bank_transaction_id`, `gst_return_id` |
| `posting_key` | generated `VARCHAR(80)` | | `CONCAT(journal_source, ':', COALESCE(<the 16 FKs>, reverses_journal_entry_id))` STORED; NULL for OPENING/RECURRING/MANUAL entries (which carry no source) |
| `created_by_employee_id` | `BIGINT UNSIGNED` | | FK → `employees`; NULL = system |
| `generation_run_id` | `BIGINT UNSIGNED` | ✓ | FK → `generation_runs` (1G) |

- **Keys:** UNIQUE `(posting_key)`: no event is posted twice (D50). UNIQUE `(reverses_journal_entry_id)`: an entry is reversed at most once.
- **Constraints:**
  - CHECK `total_debit = total_credit AND total_debit > 0`.
  - CHECK the number of non-NULL source FKs is ≤ 1 (sum-of-booleans as in 1C).
  - CHECK `journal_type IN ('OPENING','RECURRING','MANUAL') OR posting_key IS NOT NULL` (system and reversal entries must have a source).
  - CHECK `(journal_type = 'REVERSAL') = (reverses_journal_entry_id IS NOT NULL)`.
  - One CHECK per `journal_source` naming its FK, e.g. `journal_source <> 'SALES_INVOICE' OR invoice_id IS NOT NULL` (full list in §6).
  - CHECK `posting_date >= document_date OR journal_type = 'REVERSAL'`.
- **Indexes:** `(posting_date)`, `(accounting_period_id)`, `(journal_source, posting_date)`, `(created_at)` (insert watermark). Each source FK is indexed automatically.
- **Behaviour:** Insert-only, except `entry_status` POSTED → REVERSED (D61). Posted in the same DB transaction as its event.

#### `journal_entry_lines`
Debit/credit lines: **the accounting source of truth**.

| Column | Type | NN | Notes |
|---|---|---|---|
| `journal_entry_line_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `journal_entry_id` | `BIGINT UNSIGNED` | ✓ | FK → `journal_entries` |
| `line_number` | `SMALLINT UNSIGNED` | ✓ | |
| `account_id` | `INT UNSIGNED` | ✓ | FK → `chart_of_accounts` (postable only) |
| `debit_amount` | `DECIMAL(18,2)` | ✓ | Default 0 |
| `credit_amount` | `DECIMAL(18,2)` | ✓ | Default 0 |
| `cost_center_id` | `SMALLINT UNSIGNED` | | FK → `cost_centers`. Required when the account `requires_cost_center` |
| `customer_id` | `BIGINT UNSIGNED` | | FK. Required on AR control lines |
| `supplier_id` | `BIGINT UNSIGNED` | | FK. Required on AP control lines (supplier payee) |
| `employee_id` | `BIGINT UNSIGNED` | | FK. AP lines for employee claims |
| `warehouse_id` | `SMALLINT UNSIGNED` | | FK. Required on Inventory lines (stock value per warehouse reconciliation) |
| `line_description` | `VARCHAR(255)` | | |

- **Keys:** UNIQUE `(journal_entry_id, line_number)`.
- **Constraints:** CHECK `(debit_amount > 0 AND credit_amount = 0) OR (credit_amount > 0 AND debit_amount = 0)`; CHECK at most one of `customer_id`, `supplier_id`, `employee_id`.
- **Rules (validation, cross-table):** Σ debit = Σ credit = header totals (V-AC-10). Account postable and active. Cost centre present iff required. A party is set iff the account is the AR or AP control. Warehouse set iff the account is Inventory (V-AC-11).
- **Indexes:** `(account_id, journal_entry_id)`; `(customer_id)`; `(supplier_id)`; `(cost_center_id)`.
- **Behaviour:** Insert-only.

#### `v_general_ledger` (view)
The general ledger as a derived view (R6), so it never competes with journal lines.

```sql
SELECT jel.journal_entry_line_id, je.journal_entry_id, je.journal_number, je.posting_date,
       je.document_date, ap.fiscal_year, ap.period_code, je.journal_source, je.description,
       coa.account_code, coa.account_name, at.account_type_code,
       jel.debit_amount, jel.credit_amount,
       jel.debit_amount - jel.credit_amount AS net_amount,
       SUM(jel.debit_amount - jel.credit_amount) OVER (
           PARTITION BY jel.account_id
           ORDER BY je.posting_date, je.journal_entry_id, jel.line_number
           ROWS UNBOUNDED PRECEDING) AS running_balance,
       jel.cost_center_id, jel.customer_id, jel.supplier_id, jel.employee_id, jel.warehouse_id
FROM journal_entry_lines jel
JOIN journal_entries je   ON je.journal_entry_id = jel.journal_entry_id
JOIN accounting_periods ap ON ap.accounting_period_id = je.accounting_period_id
JOIN chart_of_accounts coa ON coa.account_id = jel.account_id
JOIN account_types at      ON at.account_type_id = coa.account_type_id;
```

Both POSTED and REVERSED entries are included: a reversed entry and its reversal net to zero, which is correct ledger behaviour. The running balance is debit-positive; reports flip the sign for credit-normal accounts. The DuckDB loader doesn't copy the view (1G): it's recreated or modelled in dbt.

#### `gl_period_balances`
Derived balance per account per period (R6). Rebuildable from journal lines at any time.

| Column | Type | NN | Notes |
|---|---|---|---|
| `gl_period_balance_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `account_id` | `INT UNSIGNED` | ✓ | FK → `chart_of_accounts` |
| `accounting_period_id` | `SMALLINT UNSIGNED` | ✓ | FK → `accounting_periods` |
| `opening_balance` | `DECIMAL(18,2)` | ✓ | Debit-positive |
| `period_debit` | `DECIMAL(18,2)` | ✓ | |
| `period_credit` | `DECIMAL(18,2)` | ✓ | |
| `closing_balance` | `DECIMAL(18,2)` | ✓ | |
| `rebuilt_at` | `DATETIME(6)` | ✓ | |

- **Keys:** UNIQUE `(account_id, accounting_period_id)`.
- **Constraints:** CHECK `closing_balance = opening_balance + period_debit - period_credit`; CHECK debits/credits `>= 0`.
- **Opening rule (D54):** For balance-sheet accounts, opening = the previous period's closing. For P&L accounts, opening = 0 in the first period of each FY. Retained Earnings' opening in period 1 also includes the prior FY's net P&L (a virtual close).
- **Populated:** Rebuilt for a period when it closes, and fully rebuilt on demand (`rebuild-balances` command). Rows exist for every postable account × every non-FUTURE period, so trial balances need no gap-filling. Never written by the posting engine directly.

---

## 4. Bank

#### `bank_transactions`
Bank statement lines (and petty-cash book lines), in the **bank's** sign convention: `credit_amount` = money into our account.

| Column | Type | NN | Notes |
|---|---|---|---|
| `bank_transaction_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `bank_account_id` | `SMALLINT UNSIGNED` | ✓ | FK → `bank_accounts` |
| `statement_date` | `DATE` | ✓ | Date the bank booked it |
| `value_date` | `DATE` | ✓ | |
| `transaction_type` | `VARCHAR(30)` | ✓ | `CUSTOMER_RECEIPT` / `GATEWAY_SETTLEMENT` / `COD_REMITTANCE` / `CHEQUE_DEPOSIT` / `SUPPLIER_PAYMENT` / `EXPENSE_PAYMENT` / `REFUND` / `GST_PAYMENT` / `BANK_CHARGES` / `INTEREST_CREDIT` / `INTERNAL_TRANSFER` / `REVERSAL` |
| `narration` | `VARCHAR(200)` | ✓ | Statement text ("NEFT CR-DCBK0004127-SHREE TRADERS") |
| `bank_reference` | `VARCHAR(40)` | | UTR / cheque no. / settlement batch id |
| `credit_amount` | `DECIMAL(18,2)` | ✓ | Default 0. Inflow |
| `debit_amount` | `DECIMAL(18,2)` | ✓ | Default 0. Outflow |
| `balance_after` | `DECIMAL(18,2)` | ✓ | Statement running balance |
| `customer_payment_id` | `BIGINT UNSIGNED` | | FK. Direct-to-bank receipts (NEFT/RTGS/IMPS, cleared cheques) |
| `supplier_payment_id` | `BIGINT UNSIGNED` | | FK |
| `expense_payment_id` | `BIGINT UNSIGNED` | | FK |
| `refund_id` | `BIGINT UNSIGNED` | | FK |
| `gst_return_id` | `BIGINT UNSIGNED` | | FK (GST challan payment) |
| `counter_bank_transaction_id` | `BIGINT UNSIGNED` | | FK → self, UNIQUE. The other leg of an internal transfer |
| `shipping_carrier_id` | `SMALLINT UNSIGNED` | | FK. COD remittance sender |
| `bank_reconciliation_id` | `BIGINT UNSIGNED` | | FK → `bank_reconciliations`. Set when reconciled |

- **Constraints:** CHECK `(credit_amount > 0 AND debit_amount = 0) OR (debit_amount > 0 AND credit_amount = 0)`; CHECK at most one document FK; CHECK type ↔ FK (e.g. `transaction_type <> 'SUPPLIER_PAYMENT' OR supplier_payment_id IS NOT NULL`); CHECK `(transaction_type = 'INTERNAL_TRANSFER') = (counter_bank_transaction_id IS NOT NULL)`; CHECK `(transaction_type = 'COD_REMITTANCE') = (shipping_carrier_id IS NOT NULL)`; CHECK `value_date >= statement_date - INTERVAL 3 DAY`.
- **Who posts the journal:** a line created by one of our documents (payments, refunds, GST) is **not** posted again: the document already posted, and the bank line is what reconciliation matches against. Lines originating at the bank (`GATEWAY_SETTLEMENT`, `COD_REMITTANCE`, `CHEQUE_DEPOSIT`, `BANK_CHARGES`, `INTEREST_CREDIT`, `INTERNAL_TRANSFER` outgoing leg, `REVERSAL` of a bounced item) post their own journal with `bank_transaction_id` as the source (§6).
- **Indexes:** `(bank_account_id, statement_date)`, `(bank_reconciliation_id)`, `(transaction_type, statement_date)`.
- **Populated:** Generated from the bank's point of view after the documents exist. Direct receipts and payments land on `payment_date` (NEFT/RTGS/IMPS) or after clearing (cheques). One `GATEWAY_SETTLEMENT` per bank account per method per settlement day = Σ the day's UPI/card/net-banking receipts minus fees. One `COD_REMITTANCE` per carrier per week. Monthly bank charges and quarterly interest. Petty-cash top-ups when a float falls below its threshold. `balance_after` is computed in strict `(statement_date, id)` order per account.

#### `bank_reconciliations`
Monthly reconciliation of each bank account: books vs statement.

| Column | Type | NN | Notes |
|---|---|---|---|
| `bank_reconciliation_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `bank_account_id` | `SMALLINT UNSIGNED` | ✓ | FK |
| `accounting_period_id` | `SMALLINT UNSIGNED` | ✓ | FK |
| `statement_closing_balance` | `DECIMAL(18,2)` | ✓ | `balance_after` of the last statement line in the period |
| `book_closing_balance` | `DECIMAL(18,2)` | ✓ | GL balance of the bank's GL account at period end |
| `deposits_in_transit_amount` | `DECIMAL(18,2)` | ✓ | In books, not yet on the statement |
| `unpresented_payments_amount` | `DECIMAL(18,2)` | ✓ | Issued (e.g. cheques), not yet presented |
| `unrecorded_bank_items_amount` | `DECIMAL(18,2)` | ✓ | On the statement, not yet in books (0 in clean mode) |
| `difference_amount` | `DECIMAL(18,2)` | ✓ | `book − (statement + deposits_in_transit − unpresented + unrecorded)` |
| `reconciliation_status` | `VARCHAR(30)` | ✓ | `IN_PROGRESS` / `RECONCILED` / `EXCEPTION` |
| `reconciled_by_employee_id` | `BIGINT UNSIGNED` | | FK |
| `reconciled_at` | `DATETIME(6)` | | |

- **Keys:** UNIQUE `(bank_account_id, accounting_period_id)`.
- **Constraints:** CHECK the `difference_amount` formula; CHECK `reconciliation_status <> 'RECONCILED' OR difference_amount = 0`; CHECK `(reconciliation_status = 'RECONCILED') = (reconciled_at IS NOT NULL)`.
- **Populated:** By Finance in the first week after each month end, before the period closes. Clean mode always reaches RECONCILED with explained timing items. `EXCEPTION` exists for anomaly mode (Phase 7).

---

## 5. Receivables, Payables and Tax

#### `accounts_receivable`
Open-item AR subledger (D49). Signed amounts: **+ = customer owes us**.

| Column | Type | NN | Notes |
|---|---|---|---|
| `accounts_receivable_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `customer_id` | `BIGINT UNSIGNED` | ✓ | FK |
| `item_type` | `VARCHAR(20)` | ✓ | `INVOICE` (+) / `REFUND` (+) / `CREDIT_NOTE` (−) / `PAYMENT` (−) |
| `invoice_id` | `BIGINT UNSIGNED` | | FK, UNIQUE |
| `credit_note_id` | `BIGINT UNSIGNED` | | FK, UNIQUE |
| `customer_payment_id` | `BIGINT UNSIGNED` | | FK, UNIQUE (SUCCESS payments only) |
| `refund_id` | `BIGINT UNSIGNED` | | FK, UNIQUE (COMPLETED refunds only) |
| `document_date` | `DATE` | ✓ | |
| `due_date` | `DATE` | ✓ | Invoice due date; = document date for others |
| `original_amount` | `DECIMAL(18,2)` | ✓ | Signed |
| `open_amount` | `DECIMAL(18,2)` | ✓ | Signed; reduced by allocations (D32 counter) |
| `item_status` | `VARCHAR(20)` | ✓ | `OPEN` / `PARTIALLY_SETTLED` / `SETTLED` / `REVERSED` |
| `settled_at` | `DATETIME(6)` | | When `open_amount` reached 0 |
| `journal_entry_id` | `BIGINT UNSIGNED` | ✓ | FK → the journal that posted the item |

- **Constraints:** Exactly one source FK, matching `item_type`; CHECK sign by type (`INVOICE`/`REFUND` ⇒ `original_amount > 0`, others `< 0`); CHECK `open_amount` has the same sign as `original_amount` or is 0, and `ABS(open_amount) <= ABS(original_amount)`; CHECK `(open_amount = 0) = (item_status IN ('SETTLED','REVERSED'))`; CHECK `(item_status = 'SETTLED') = (settled_at IS NOT NULL)`.
- **Ageing:** Derived (as-of date − `due_date`) for OPEN invoice items. It's never stored, so it can't go stale.
- **Rules:** Σ `open_amount` (excluding REVERSED) per customer = that customer's balance on AR control in the GL (V-AR-01). `original_amount` = the document total (V-AR-02). A REVERSED payment item has open 0 and its allocations reversed.
- **Indexes:** `(customer_id, item_status)`, `(item_status, due_date)` (ageing).

#### `customer_payment_allocations`
Clearing of AR items (R1: the single "payment X settles invoice Y" table). Two kinds: **payment → invoice** and **credit note → invoice or refund**.

| Column | Type | NN | Notes |
|---|---|---|---|
| `customer_payment_allocation_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `customer_payment_id` | `BIGINT UNSIGNED` | | FK. Credit side (payment) |
| `credit_note_id` | `BIGINT UNSIGNED` | | FK. Credit side (credit note) |
| `invoice_id` | `BIGINT UNSIGNED` | | FK. Debit side |
| `refund_id` | `BIGINT UNSIGNED` | | FK. Debit side (only with a credit note) |
| `allocated_amount` | `DECIMAL(18,2)` | ✓ | > 0 |
| `allocation_date` | `DATE` | ✓ | |
| `allocated_by_employee_id` | `BIGINT UNSIGNED` | | FK; NULL = automatic |
| `allocation_status` | `VARCHAR(20)` | ✓ | `ACTIVE` / `REVERSED` |
| `reversed_at` | `DATETIME(6)` | | |

- **Constraints:** CHECK exactly one credit side and exactly one debit side; CHECK `refund_id IS NULL OR credit_note_id IS NOT NULL`; CHECK `allocated_amount > 0`; CHECK `(allocation_status = 'REVERSED') = (reversed_at IS NOT NULL)`.
- **Rules:** Same customer on both sides. Only SUCCESS payments. Σ ACTIVE allocations per item ≤ its original amount, and AR `open_amount` = original − Σ (V-AR-03). `allocation_date` ≥ both documents' dates.
- **Indexes:** `(invoice_id)`, `(customer_payment_id)`, `(credit_note_id)`.
- **Populated:** Prepayments, counter and COD payments are allocated to their order's invoice(s) as soon as each invoice exists. B2B payments are allocated oldest-due-first or to the invoices the customer named. A credit note is allocated to its own invoice up to the invoice's open amount; any remainder goes to a refund (B2C) or to the customer's next invoices (B2B).

#### `accounts_payable`
Open-item AP subledger (D49, D51). Signed amounts: **+ = we owe**.

| Column | Type | NN | Notes |
|---|---|---|---|
| `accounts_payable_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `supplier_id` | `BIGINT UNSIGNED` | | FK. Payee (supplier) |
| `employee_id` | `BIGINT UNSIGNED` | | FK. Payee (employee claim) |
| `item_type` | `VARCHAR(20)` | ✓ | `SUPPLIER_INVOICE` (+) / `EXPENSE` (+) / `SUPPLIER_PAYMENT` (−) |
| `supplier_invoice_id` | `BIGINT UNSIGNED` | | FK, UNIQUE |
| `expense_id` | `BIGINT UNSIGNED` | | FK, UNIQUE (non-payroll, D56/D57) |
| `supplier_payment_id` | `BIGINT UNSIGNED` | | FK, UNIQUE (COMPLETED) |
| `document_date` | `DATE` | ✓ | |
| `due_date` | `DATE` | ✓ | |
| `original_amount` | `DECIMAL(18,2)` | ✓ | Signed |
| `open_amount` | `DECIMAL(18,2)` | ✓ | Signed |
| `item_status` | `VARCHAR(20)` | ✓ | `OPEN` / `PARTIALLY_SETTLED` / `SETTLED` / `REVERSED` |
| `settled_at` | `DATETIME(6)` | | |
| `journal_entry_id` | `BIGINT UNSIGNED` | ✓ | FK |

- **Constraints:** Exactly one payee (CHECK); exactly one source FK matching `item_type`; CHECK `employee_id IS NULL OR item_type = 'EXPENSE'`; sign and open-amount CHECKs as in AR.
- **Rules:** Σ open per payee = GL AP control by party (V-AP-01). An expense item's open = total − Σ COMPLETED `expense_payments`. A supplier invoice item's open = total − Σ ACTIVE allocations. MSME items paid within 45 days (V-MD-14 applied).
- **Indexes:** `(supplier_id, item_status)`, `(item_status, due_date)`.

#### `supplier_payment_allocations`

| Column | Type | NN | Notes |
|---|---|---|---|
| `supplier_payment_allocation_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `supplier_payment_id` | `BIGINT UNSIGNED` | ✓ | FK (COMPLETED) |
| `supplier_invoice_id` | `BIGINT UNSIGNED` | ✓ | FK |
| `allocated_amount` | `DECIMAL(18,2)` | ✓ | > 0 |
| `allocation_date` | `DATE` | ✓ | |
| `allocation_status` | `VARCHAR(20)` | ✓ | `ACTIVE` / `REVERSED` |
| `reversed_at` | `DATETIME(6)` | | |

- **Keys:** UNIQUE `(supplier_payment_id, supplier_invoice_id)`.
- **Rules:** Same supplier. Σ per payment ≤ payment amount; Σ per invoice ≤ invoice total; AP open amounts follow (V-AP-02). Advances are allocated once the invoice arrives.

#### `tax_transactions`
GST per taxable document line and direction: the basis for GST reports and returns.

| Column | Type | NN | Notes |
|---|---|---|---|
| `tax_transaction_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `company_gst_registration_id` | `SMALLINT UNSIGNED` | ✓ | FK. Our GSTIN on this side of the transaction |
| `tax_direction` | `VARCHAR(10)` | ✓ | `OUTPUT` / `INPUT` |
| `document_type` | `VARCHAR(30)` | ✓ | `SALES_INVOICE` / `CREDIT_NOTE` / `SUPPLIER_INVOICE` / `EXPENSE` / `TRANSFER_OUT` / `TRANSFER_IN` |
| `invoice_line_item_id` | `BIGINT UNSIGNED` | | Source FK |
| `credit_note_line_item_id` | `BIGINT UNSIGNED` | | Source FK |
| `supplier_invoice_line_item_id` | `BIGINT UNSIGNED` | | Source FK |
| `expense_line_item_id` | `BIGINT UNSIGNED` | | Source FK |
| `stock_transfer_line_item_id` | `BIGINT UNSIGNED` | | Source FK (two rows per line: OUT at source, IN at destination) |
| `document_number` | `VARCHAR(40)` | ✓ | Snapshot (invoice / credit note / bill number) |
| `document_date` | `DATE` | ✓ | |
| `return_period` | generated `CHAR(7)` | | `DATE_FORMAT(document_date, '%Y-%m')` STORED |
| `counterparty_gstin` | `CHAR(15)` | | Snapshot; NULL for unregistered parties |
| `customer_id` | `BIGINT UNSIGNED` | | FK (output side) |
| `supplier_id` | `BIGINT UNSIGNED` | | FK (input side) |
| `place_of_supply_state_id` | `SMALLINT UNSIGNED` | ✓ | FK → `states` |
| `is_inter_state` | `BOOLEAN` | ✓ | |
| `hsn_code` | `VARCHAR(8)` | ✓ | HSN or SAC |
| `uqc` | `VARCHAR(3)` | | Unit quantity code (goods) |
| `quantity` | `DECIMAL(18,3)` | | |
| `taxable_value` | `DECIMAL(18,2)` | ✓ | Signed: credit notes negative |
| `gst_rate` | `DECIMAL(7,4)` | ✓ | |
| `cgst_amount`, `sgst_amount`, `igst_amount` | `DECIMAL(18,2)` | ✓ | Signed like the taxable value |
| `itc_eligibility` | `VARCHAR(20)` | ✓ | `ELIGIBLE` / `INELIGIBLE` / `NOT_APPLICABLE` (output) |
| `gstr1_section` | `VARCHAR(10)` | | `B2B` / `B2CL` / `B2CS` / `CDNR` / `CDNUR` (output only) |
| `gst_return_id` | `BIGINT UNSIGNED` | | FK → the GSTR-1 (output) or GSTR-3B (input) return that included it |

- **Keys:** UNIQUE per source FK + direction (`UNIQUE (stock_transfer_line_item_id, tax_direction)`, `UNIQUE (invoice_line_item_id)`, and so on).
- **Constraints:** Exactly one source FK, consistent with `document_type` and direction; CHECK `(tax_direction = 'OUTPUT') = (itc_eligibility = 'NOT_APPLICABLE')`; CHECK `(tax_direction = 'OUTPUT') = (gstr1_section IS NOT NULL)`; 1C §2.3 split rules (with sign).
- **GSTR-1 section rule:** `B2B` if the buyer is registered (including our own GSTINs for transfers); `B2CL` if unregistered, inter-state and invoice value > ₹1,00,000; else `B2CS`. Credit notes: `CDNR` or `CDNUR`. The threshold is config (the rule changed in 2024).
- **Rules:** Amounts equal the source line's (V-TX-01). Σ per GSTIN per period per component per direction = the movement in that GSTIN's tax accounts in the GL (V-TX-02).
- **Indexes:** `(company_gst_registration_id, return_period, tax_direction)`, `(hsn_code)`, `(gst_return_id)`.
- **Populated:** In the same transaction as the source document's posting (supplier invoice approved, expense approved, invoice issued, credit note issued, transfer dispatched). A cancelled invoice gets **negative mirror rows**, not deletes, so the tax history stays append-only. `gst_return_id` is set at filing (the only update).

#### `gst_returns`

| Column | Type | NN | Notes |
|---|---|---|---|
| `gst_return_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `company_gst_registration_id` | `SMALLINT UNSIGNED` | ✓ | FK |
| `return_type` | `VARCHAR(10)` | ✓ | `GSTR1` / `GSTR3B` |
| `return_period` | `CHAR(7)` | ✓ | `2026-04` |
| `due_date` | `DATE` | ✓ | 11th (GSTR-1) / 20th (GSTR-3B) of the next month |
| `filed_date` | `DATE` | | |
| `arn` | `VARCHAR(20)` | | UNIQUE. Fictional acknowledgement reference |
| `output_cgst`, `output_sgst`, `output_igst` | `DECIMAL(18,2)` | ✓ | Period output tax (net of credit notes) |
| `itc_cgst`, `itc_sgst`, `itc_igst` | `DECIMAL(18,2)` | ✓ | Eligible input tax claimed (3B only, else 0) |
| `itc_used_cgst`, `itc_used_sgst`, `itc_used_igst` | `DECIMAL(18,2)` | ✓ | Credit used by each credit type (3B) |
| `cash_paid_cgst`, `cash_paid_sgst`, `cash_paid_igst` | `DECIMAL(18,2)` | ✓ | Cash paid per component (3B) |
| `payment_date` | `DATE` | | |
| `bank_account_id` | `SMALLINT UNSIGNED` | | FK. Payment account |
| `challan_reference` | `VARCHAR(20)` | | Fictional payment challan reference |
| `return_status` | `VARCHAR(20)` | ✓ | `DRAFT` / `FILED` / `FILED_LATE` |

- **Keys:** UNIQUE `(company_gst_registration_id, return_type, return_period)`.
- **Constraints:** CHECK lists; CHECK `return_status = 'DRAFT' OR (filed_date IS NOT NULL AND arn IS NOT NULL)`; CHECK `(return_status = 'FILED_LATE') = (filed_date > due_date)`; CHECK `return_type = 'GSTR3B' OR (all itc_* = 0 AND all cash_paid_* = 0)`; CHECK all amounts `>= 0`; CHECK `itc_used_cgst + itc_used_sgst + itc_used_igst + cash_paid_cgst + cash_paid_sgst + cash_paid_igst = output_cgst + output_sgst + output_igst` (3B; 0 = 0 for GSTR-1). The per-head identity (credit can cross heads) is checked in validation from the section `6.1` lines, which record which credit or cash paid which head (`paid_through`).
- **Set-off order (D58, legal rule):** IGST credit pays IGST first, then CGST and SGST. CGST credit pays CGST, then IGST. SGST credit pays SGST, then IGST. CGST credit can never pay SGST or vice versa. Cash pays the remainder. Unused credit carries forward (it stays in the Input GST GL accounts).
- **Populated:** Monthly per GSTIN. GSTR-1 on or before the 11th; GSTR-3B and payment on or before the 20th (~3% filed late in clean mode, a legitimate business event). The 3B filing posts the settlement journal and creates the `GST_PAYMENT` bank line.

#### `gst_return_line_items`
Summarised sections of a return, rebuilt from `tax_transactions`.

| Column | Type | NN | Notes |
|---|---|---|---|
| `gst_return_line_item_id` | `BIGINT UNSIGNED` | ✓ | PK |
| `gst_return_id` | `BIGINT UNSIGNED` | ✓ | FK |
| `line_number` | `SMALLINT UNSIGNED` | ✓ | |
| `section_code` | `VARCHAR(20)` | ✓ | GSTR-1: `B2B`, `B2CL`, `B2CS`, `CDNR`, `CDNUR`, `HSN`. GSTR-3B: `3.1A` (outward taxable), `4A5` (all other ITC), `4B` (ITC reversed / ineligible), `6.1` (payment of tax) |
| `gst_rate` | `DECIMAL(7,4)` | | Per-rate rows |
| `hsn_code` | `VARCHAR(8)` | | HSN summary rows |
| `place_of_supply_state_id` | `SMALLINT UNSIGNED` | | FK (B2CS / B2CL by state) |
| `paid_through` | `VARCHAR(10)` | | Section `6.1` only: `ITC_IGST` / `ITC_CGST` / `ITC_SGST` / `CASH`. The amount columns then say which tax head it paid |
| `document_count` | `INT UNSIGNED` | ✓ | |
| `taxable_value` | `DECIMAL(18,2)` | ✓ | |
| `cgst_amount`, `sgst_amount`, `igst_amount` | `DECIMAL(18,2)` | ✓ | |

- **Keys:** UNIQUE `(gst_return_id, line_number)`.
- **Constraints:** CHECK `(section_code = '6.1') = (paid_through IS NOT NULL)`; CHECK `paid_through <> 'ITC_CGST' OR sgst_amount = 0`; CHECK `paid_through <> 'ITC_SGST' OR cgst_amount = 0` (the legal cross-utilisation ban).
- **Rules:** Σ lines per section = Σ the included `tax_transactions` (V-TX-03). Section codes are valid for the return type. Σ `6.1` lines by `paid_through` = the header's `itc_used_*` / `cash_paid_*`; credit used ≤ credit available incl. carry-forward.

---

## 6. Double-Entry Posting Rules

One journal per event, in the same DB transaction (D50). **CC** = cost centre (1B §4.9 usage rule): BU CC for revenue, COGS, returns and PPV; warehouse CC for stock write-offs and site costs; the line's own CC for expenses; the Finance department CC for bank and gateway charges; CORP for depreciation. Party columns on AR/AP lines; warehouse on Inventory lines. GST accounts are always those of the GSTIN named.

| # | Event (`journal_source`) | Source FK | Debit | Credit |
|---|---|---|---|---|
| P01 | Opening balances (`OPENING_BALANCE`, type OPENING) | — | Bank / petty cash (opening cash), Fixed assets | Share Capital, Accumulated Depreciation, Opening Balance Equity (balancing) |
| P02 | Opening stock (`STOCK_ADJUSTMENT`, adj. type OPENING_STOCK) | `stock_adjustment_id` | Inventory (per warehouse) | Opening Balance Equity |
| P03 | Goods receipt inspected (`GOODS_RECEIPT`) | `goods_receipt_id` | Inventory (accepted qty × PO net price) | GRNI |
| P04 | Supplier invoice approved (`SUPPLIER_INVOICE`) | `supplier_invoice_id` | GRNI (invoiced qty × PO price); PPV (if invoice price > PO); Input CGST/SGST/IGST (bill-to GSTIN, registered suppliers only) | AP (supplier, invoice total); PPV (if invoice price < PO); Rounding ± |
| P05 | Supplier payment completed (`SUPPLIER_PAYMENT`) | `supplier_payment_id` | AP (supplier) | Bank |
| P06 | Sales invoice issued (`SALES_INVOICE`), incl. COGS (D52) | `invoice_id` | AR (customer, total); **COGS – BU** (Σ issue values); Rounding ± | **Sales – BU** (taxable per BU); Shipping Income; Output CGST/SGST or IGST (issuing GSTIN); **Inventory** (warehouse, Σ issue values) |
| P07 | Invoice cancelled (`REVERSAL`, D40) | `reverses_journal_entry_id` → P06 | Mirror of P06 | Mirror of P06 |
| P08 | Customer payment succeeded (`CUSTOMER_PAYMENT`) | `customer_payment_id` | Bank (NEFT/RTGS/IMPS/cleared cheque), Petty cash (store cash), or **Payment Clearing** (UPI/card/net banking/COD) (D53) | AR (customer) |
| P09 | Payment reversed / chargeback (`REVERSAL`) | → P08 | AR (customer) | Bank or Clearing (mirror) |
| P10 | Gateway settlement / COD remittance (`BANK_SETTLEMENT`) | `bank_transaction_id` | Bank (net received); Payment Gateway Charges (fees) | Payment Clearing (gross of the settled payments) |
| P11 | Credit note issued (`CREDIT_NOTE`) | `credit_note_id` | **Sales Returns – BU** (taxable); Output CGST/SGST or IGST (invoice GSTIN); Rounding ± | AR (customer, total) |
| P12 | Return goods received (`SALES_RETURN_RECEIPT`) | `sales_return_id` | Inventory (return warehouse, received qty × original issue cost) | COGS – BU |
| P13 | RTO goods received (`RTO_RECEIPT`) | `shipment_id` | Inventory (warehouse, at original issue cost) | COGS – BU |
| P14 | Refund completed (`REFUND`) | `refund_id` | AR (customer) | Bank |
| P15 | Stock adjustment posted (`STOCK_ADJUSTMENT`) | `stock_adjustment_id` | Losses: Inventory Write-offs (warehouse CC). Gains: Inventory | Losses: Inventory. Gains: Inventory Adjustment Gains. (Pure S → D reclasses have zero value and post no lines; an adjustment with zero net value posts no journal) |
| P16 | Transfer dispatched (`TRANSFER_DISPATCH`) | `stock_transfer_id` | Inventory in Transit; Input IGST (destination GSTIN) if inter-GSTIN (D55) | Inventory (source warehouse); Output IGST (source GSTIN) if inter-GSTIN |
| P17 | Transfer received (`TRANSFER_RECEIPT`) | `stock_transfer_id` | Inventory (destination, good + damaged at transfer cost); Inventory Write-offs (lost qty) | Inventory in Transit |
| P18 | Defective stock disposed (`DEFECTIVE_DISPOSITION`) | `defective_stock_id` | Inventory Write-offs (SCRAP, RETURN_TO_SUPPLIER) | Inventory (warehouse). REPAIR posts nothing (value unchanged; cost is a memo, D60) |
| P19 | Expense approved (`EXPENSE`) | `expense_id` | Expense account per line (taxable + ineligible GST, line CC); Input GST (eligible part, bill-to GSTIN); Rounding ± | AP (supplier or employee). **Payroll:** Salaries & Wages per CC / Salaries Payable |
| P20 | Expense payment completed (`EXPENSE_PAYMENT`) | `expense_payment_id` | AP (payee), or Salaries Payable for payroll | Bank or Petty cash |
| P21 | Bank-originated items (`BANK_CHARGES`, `INTEREST`, `INTERNAL_TRANSFER`) | `bank_transaction_id` | Bank Charges (Finance CC) / Bank / destination bank or petty cash | Bank / Interest Income / source bank |
| P22 | GSTR-3B filed and paid (`GST_SETTLEMENT`, D58) | `gst_return_id` | Output CGST, SGST, IGST (period output tax of that GSTIN) | Input CGST, SGST, IGST (credit used); Bank (cash paid) |
| P23 | Monthly depreciation (`DEPRECIATION`, type RECURRING) | — | Depreciation (CORP CC) | Accumulated Depreciation |
| P24 | Manual adjustment (`MANUAL_ADJUSTMENT`, type MANUAL) | — | As entered (must still balance; never on control accounts, 1B) | As entered |
| P25 | Any correction (`REVERSAL`) | `reverses_journal_entry_id` | Mirror of the original | Mirror of the original |

**Not posted** (deliberately): RFQs, quotations, POs, reservations, shipments before dispatch, failed payments, allocations (D49), status changes, pure S → D stock reclasses, and inspections that don't move value.

### Worked example (intra-state B2B sale and collection)

Invoice `KA2627-I000123` from DC Bengaluru (GSTIN 29…) to a registered Bengaluru retailer: 10 power banks at ₹1,000, 18% GST; moving-average cost ₹720.

| Journal | Account | Dr | Cr |
|---|---|---|---|
| P06 | Trade Receivables (customer) | 11,800.00 | |
| | Sales – Electronics (BU-ELEC) | | 10,000.00 |
| | Output CGST – Karnataka | | 900.00 |
| | Output SGST – Karnataka | | 900.00 |
| | COGS – Electronics (BU-ELEC) | 7,200.00 | |
| | Inventory (DC-BLR) | | 7,200.00 |
| | **Totals** | **19,000.00** | **19,000.00** |
| P08 (NEFT 30 days later) | Bank – Collections | 11,800.00 | |
| | Trade Receivables (customer) | | 11,800.00 |

The allocation (payment → invoice) posts nothing. It sets both AR items' `open_amount` to 0. The ₹1,800 of output GST stays in the liability accounts until the GSTR-3B settlement (P22) for that month offsets it against input credit and the cash payment.

---

## 7. Reconciliations the Design Guarantees

These are the master-prompt reconciliations (§10, §15E) for this part. Each is a validation in Phase 3 and must pass in clean mode.

| ID | Check |
|---|---|
| V-AC-10 | Every journal: Σ debit = Σ credit = header totals; one journal per (source, event) (posting key) |
| V-AC-11 | Line rules: postable account; cost centre iff required; party iff AR/AP control; warehouse iff Inventory |
| V-AC-12 | Every financially relevant event has its journal (§6) and no journal lacks its event, in both directions |
| V-AC-13 | `gl_period_balances` = re-aggregation of journal lines; trial balance totals equal (Σ debit balances = Σ credit balances) for every period |
| V-AC-14 | Postings only into OPEN periods; `posting_date` inside the period; D59 rule holds |
| V-AR-01 | AR subledger (Σ open per customer) = GL AR control per customer, at every period end |
| V-AR-02 | AR item amounts = their documents' totals; every ISSUED invoice, credit note, SUCCESS payment and COMPLETED refund has exactly one AR item |
| V-AR-03 | Open amounts = original − Σ ACTIVE allocations; allocations same customer, within amounts |
| V-AP-01 | AP subledger = GL AP control per payee |
| V-AP-02 | Supplier invoices vs AP: one AP item per approved supplier invoice / non-payroll expense / completed supplier payment; open amounts consistent with allocations and expense payments |
| V-INV-GL | Σ `inventory.stock_value` per warehouse = GL Inventory per warehouse; GL Inventory in Transit = open transfer value (1C §4) |
| V-GRNI | GRNI balance = Σ (accepted − invoiced) qty × PO price over open PO lines |
| V-CLR | Payment Clearing balance = Σ SUCCESS clearing-method payments with no `settlement_bank_transaction_id` yet |
| V-BK-01 | Every completed payment, refund and GST payment has exactly one bank line with the same amount; bank `balance_after` chains correctly |
| V-BK-02 | Every month × bank account has a RECONCILED reconciliation with difference 0 |
| V-TX-01 | Tax transactions = source line tax amounts, one per taxable line (two per inter-GSTIN transfer line) |
| V-TX-02 | Σ tax transactions per GSTIN × period × component × direction = movement in that GSTIN's tax accounts |
| V-TX-03 | Return line items = Σ included tax transactions; 3B: output − credit used − cash paid = 0 per head; set-off order respected; P22 journal = 3B amounts |
| V-EX-01 | Expense header = Σ lines; ITC-eligible amount = Σ eligible lines' GST |
| V-EX-02 | Category, cost-centre and campaign rules |
| V-EX-03 | Payment source and amount rules; Σ payments ≤ total; payroll = Σ active salaries |

---

## 8. Relationship Summary (new tables)

| Child | Column(s) | Parent | Card. |
|---|---|---|---|
| `expenses` | `supplier_id` / `employee_id` / `company_gst_registration_id` / `warehouse_id` / approvers | … | N:1 |
| `expense_line_items` | `expense_id` / `expense_category_id` / `cost_center_id` / `marketing_campaign_id` | … | N:1 |
| `expense_payments` | `expense_id` / `bank_account_id` / `payment_method_id` | … | N:1 |
| `expense_attachments_metadata` | `expense_id` / `uploaded_by_employee_id` | … | N:1 |
| `journal_entries` | 16 source FKs (≤ 1 set) / `accounting_period_id` / `reverses_journal_entry_id` / `generation_run_id` | documents / periods / self / runs | N:1 (self 0..1:1) |
| `journal_entry_lines` | `journal_entry_id` / `account_id` / `cost_center_id` / party FKs / `warehouse_id` | … | N:1 |
| `gl_period_balances` | `account_id` / `accounting_period_id` | … | N:1 (UNIQUE pair) |
| `bank_transactions` | `bank_account_id`; ≤ 1 document FK; `counter_bank_transaction_id`; `shipping_carrier_id`; `bank_reconciliation_id` | … | N:1 (self 1:1) |
| `bank_reconciliations` | `bank_account_id` / `accounting_period_id` | … | N:1 (UNIQUE pair) |
| `accounts_receivable` | `customer_id`; exactly one of invoice / credit note / payment / refund; `journal_entry_id` | … | 1:1 with its document |
| `customer_payment_allocations` | (payment ⊕ credit note) → (invoice ⊕ refund) | … | M:N bridge |
| `accounts_payable` | payee (supplier ⊕ employee); exactly one of supplier invoice / expense / supplier payment; `journal_entry_id` | … | 1:1 with its document |
| `supplier_payment_allocations` | `supplier_payment_id` / `supplier_invoice_id` | … | M:N bridge |
| `tax_transactions` | exactly one source line FK; GSTIN; party; `gst_return_id` | … | 1:1 (2:1 for transfers) |
| `gst_returns` | `company_gst_registration_id` / `bank_account_id` | … | N:1 |
| `gst_return_line_items` | `gst_return_id` | … | N:1 |
| `customer_payments` (1D, D63) | `settlement_bank_transaction_id` | `bank_transactions` | N:1 |

---

## 9. Change Behaviour and Volumes

| Behaviour | Tables |
|---|---|
| Insert-only | `journal_entry_lines`, `expense_line_items`, `expense_attachments_metadata`, `gst_return_line_items` |
| Insert + one-way status update | `journal_entries` (→ REVERSED), `tax_transactions` (`gst_return_id` set), `bank_transactions` (reconciliation link), allocations (→ REVERSED) |
| Mutable | `expenses`, `expense_payments`, `accounts_receivable`, `accounts_payable` (open amounts), `bank_reconciliations`, `gst_returns` |
| Derived / rebuilt | `gl_period_balances` (full reload candidate, 1G) |

**Indicative volumes (Medium, 2 years):**

| Table | Rows |
|---|---|
| `expenses` / lines / payments / attachments | ~5,500 / ~8,000 / ~5,800 / ~7,500 |
| `journal_entries` / lines | ~150,000 / ~800,000 |
| `gl_period_balances` | ~3,100 (≈130 accounts × 24 periods) |
| `bank_transactions` / `bank_reconciliations` | ~35,000 / ~260 |
| `accounts_receivable` / `customer_payment_allocations` | ~118,000 / ~65,000 |
| `accounts_payable` / `supplier_payment_allocations` | ~15,000 / ~6,500 |
| `tax_transactions` | ~185,000 |
| `gst_returns` / lines | 240 (5 GSTINs × 2 types × 24) / ~6,000 |

`journal_entry_lines` is the largest table in the database. Together with `inventory_transactions`, it's the best candidate for partition-style incremental loading.

---

## 10. What Comes Next

Part 1F: ERD by module (Mermaid diagrams for all 92 tables, plus a cross-module overview).
