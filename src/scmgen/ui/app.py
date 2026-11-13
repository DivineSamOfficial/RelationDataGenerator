"""scmgen control panel (Phase 5; D12; 1A §8.4). Run with ``scmgen ui`` (or ``streamlit run`` on this file).

A thin layer over the same engine the CLI uses: actions start ``scmgen`` commands as background jobs
(``scmgen.ui.jobs``); progress, results and data are read from MySQL, the validation reports and the DuckDB replica.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st
from sqlalchemy import text

from scmgen.core.settings import load_settings
from scmgen.db.connection import create_mysql_engine
from scmgen.db.workspaces import list_workspaces
from scmgen.ui import jobs
from scmgen.ui.guide import join_example, load_examples, load_table_docs, relations_dot

st.set_page_config(page_title="scmgen control panel", page_icon="📦", layout="wide")
PAGES = ("Workspaces", "Run monitor", "Data explorer", "Validation", "Scenarios", "DuckDB load", "Table guide",
         "Reset")


# ------------------------------------------------------------------------------------------ shared


@st.cache_resource
def settings():
    return load_settings()


@st.cache_resource
def engine(schema: str | None):
    return create_mysql_engine(settings(), role="app", database=schema)


def query(schema: str, sql: str, **params) -> pd.DataFrame:
    with engine(schema).connect() as conn:
        result = conn.execute(text(sql), params)
        return pd.DataFrame(result.fetchall(), columns=list(result.keys()))


def workspaces():
    try:
        return list_workspaces(settings())
    except Exception as exc:                      # MySQL down or not initialised
        st.error(f"Can't read the workspace registry: {exc}")
        return []


def start_job(label: str, args: list[str]) -> None:
    job = jobs.start(label, args)
    st.success(f"Started in the background: {label} (job {job.job_id}). Follow it on this page or in Run monitor.")


def job_panel(filter_text: str | None = None) -> None:
    recent = [j for j in jobs.recent() if not filter_text or filter_text in " ".join(j.command)]
    if not recent:
        return
    st.subheader("Background jobs")
    for job in recent[:6]:
        state = "running" if job.running else (f"finished (exit {job.exit_code})" if job.exit_code is not None else "ended")
        with st.expander(f"{job.label} — {state} — {job.job_id}", expanded=job.running):
            st.code(job.tail() or "(no output yet)", language=None)


# ------------------------------------------------------------------------------------------ pages


def page_workspaces(selected) -> None:
    st.header("Workspaces")
    rows = workspaces()
    if rows:
        st.dataframe(pd.DataFrame([{
            "code": w.code, "company": w.company_name, "industries": ", ".join(w.industries), "status": w.status,
            "seed": w.base_seed, "committed through": w.last_committed_through, "build": w.build_id[:8]}
            for w in rows]), hide_index=True, width="stretch")
    with st.expander("Create a workspace", expanded=not rows):
        with st.form("create"):
            code = st.text_input("Code (2–10 lower-case letters / digits)", max_chars=10)
            company = st.text_input("Company name (fictional)", "Kaveri Nexus Distribution Pvt. Ltd.")
            from scmgen.generation.packs import available_packs
            industries = st.multiselect("Industries", available_packs(), default=["electronics", "fmcg"])
            seed = st.number_input("Random seed", min_value=0, value=42, step=1)
            if st.form_submit_button("Create"):
                start_job(f"create {code}", ["workspace", "create", "--code", code, "--company", company,
                                             "--industries", ",".join(industries), "--seed", str(int(seed))])
    if selected is None:
        job_panel()
        return
    st.subheader(f"Generate — {selected.code}")
    left, right = st.columns(2)
    with left:
        st.markdown("**Historical generation** (first run)")
        preset = st.selectbox("Volume preset", ["small", "medium", "large"], index=0)
        start = st.date_input("Start date", date.today() - timedelta(days=365))
        end = st.date_input("End date", date.today() - timedelta(days=1))
        from scmgen.generation.scenarios import load_scenarios
        profile = st.selectbox("Scenario profile", list(load_scenarios()["profiles"]))
        if st.button("Generate history", type="primary"):
            args = ["generate", "history", "-w", selected.code, "--preset", preset,
                    "--set", f"history.start_date={start}", "--set", f"history.end_date={end}"]
            if profile != "default":
                args += ["--scenario", profile]
            start_job(f"history {selected.code}", args)
    with right:
        st.markdown("**Continue** (same settings, up to yesterday or N more days)")
        days = st.number_input("Days (0 = up to yesterday)", min_value=0, value=0, step=1)
        try:
            from scmgen.generation.actions import plan
            preview = plan(settings(), selected.code, days=int(days) or None)
            st.caption(f"Plan: {preview.days} day(s) {preview.first_day or ''} … {preview.last_day or ''}"
                       + (f" — blocked: {preview.blocked}" if preview.blocked else ""))
        except Exception as exc:
            st.caption(f"Plan unavailable: {exc}")
        if st.button("Continue"):
            start_job(f"continue {selected.code}", ["generate", "continue", "-w", selected.code]
                      + (["--days", str(int(days))] if days else []))
        st.markdown("**Rebuild** (drop and regenerate the same history, same seed)")
        confirm = st.text_input("Type the workspace code to confirm a rebuild")
        if st.button("Rebuild", disabled=confirm != selected.code):
            start_job(f"rebuild {selected.code}", ["generate", "rebuild", "-w", selected.code, "--confirm", confirm])
    job_panel(selected.code)


def page_monitor(selected) -> None:
    st.header("Run monitor")
    if selected is None:
        st.info("Pick a workspace in the sidebar.")
        return

    @st.fragment(run_every=5)
    def live():
        runs = query(selected.oltp_schema, """
            SELECT generation_run_id, run_number, run_mode, run_status, sim_start_date, sim_end_date,
                   committed_through, started_at, finished_at, error_message
            FROM generation_runs ORDER BY generation_run_id DESC LIMIT 10""")
        if runs.empty:
            st.info("No generation run yet.")
            return
        latest = runs.iloc[0]
        total = (latest["sim_end_date"] - latest["sim_start_date"]).days + 1
        done = 0 if pd.isna(latest["committed_through"]) else max(0, (latest["committed_through"].date() - latest["sim_start_date"]).days + 1)
        st.metric("Latest run", f"#{latest['run_number']} {latest['run_status']}", f"{min(done, total)} / {total} days")
        st.progress(min(1.0, done / total if total else 1.0))
        steps = query(selected.oltp_schema, """
            SELECT step_name, step_status, planned_units, completed_units, generated_rows, rows_per_second,
                   started_at, finished_at FROM generation_run_steps WHERE generation_run_id = :r ORDER BY step_order""",
                      r=int(latest["generation_run_id"]))
        st.dataframe(steps, hide_index=True, width="stretch")
        if latest["run_status"] == "RUNNING" and st.button("Cancel after the current day"):
            with engine(selected.oltp_schema).begin() as conn:
                conn.execute(text("UPDATE generation_runs SET cancel_requested = TRUE WHERE generation_run_id = :r"),
                             {"r": int(latest["generation_run_id"])})
            st.warning("Cancel requested: the run stops after the day in progress (D64).")
        st.subheader("Runs")
        st.dataframe(runs, hide_index=True, width="stretch")
    live()
    job_panel(selected.code)


def page_explorer(selected) -> None:
    st.header("Data explorer")
    if selected is None:
        st.info("Pick a workspace in the sidebar.")
        return
    schema = selected.oltp_schema
    tab_counts, tab_sample, tab_charts = st.tabs(["Row counts", "Sample rows", "Charts"])
    with tab_counts:
        if st.button("Count rows (exact)"):
            tables = query(schema, "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() "
                                   "AND TABLE_TYPE = 'BASE TABLE' ORDER BY 1")["TABLE_NAME"].tolist()
            counts = [{"table": t, "rows": int(query(schema, f"SELECT COUNT(*) AS n FROM `{t}`")["n"][0])} for t in tables]
            st.dataframe(pd.DataFrame(counts).sort_values("rows", ascending=False), hide_index=True,
                         width="stretch")
    with tab_sample:
        tables = query(schema, "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() "
                               "AND TABLE_TYPE = 'BASE TABLE' ORDER BY 1")["TABLE_NAME"].tolist()
        table = st.selectbox("Table", tables, index=tables.index("orders") if "orders" in tables else 0)
        st.dataframe(query(schema, f"SELECT * FROM `{table}` ORDER BY 1 DESC LIMIT 200"), width="stretch")
    with tab_charts:
        orders = query(schema, """SELECT DATE_FORMAT(o.order_date, '%Y-%m') AS month, c.channel_name AS channel,
                                         COUNT(*) AS orders FROM orders o
                                  JOIN sales_channels c ON c.sales_channel_id = o.sales_channel_id GROUP BY 1, 2 ORDER BY 1""")
        if not orders.empty:
            st.subheader("Orders by month and sales channel")
            st.bar_chart(orders.pivot(index="month", columns="channel", values="orders").fillna(0))
        revenue = query(schema, """SELECT DATE_FORMAT(i.invoice_date, '%Y-%m') AS month, b.business_unit_name AS unit,
                                          SUM(l.taxable_amount) AS revenue
                                   FROM invoices i JOIN invoice_line_items l ON l.invoice_id = i.invoice_id
                                   JOIN products p ON p.product_id = l.product_id
                                   JOIN business_units b ON b.business_unit_id = p.business_unit_id
                                   WHERE i.invoice_status = 'ISSUED' GROUP BY 1, 2 ORDER BY 1""")
        if not revenue.empty:
            st.subheader("Revenue (taxable) by month and business unit")
            st.area_chart(revenue.pivot(index="month", columns="unit", values="revenue").fillna(0).astype(float))
        stock = query(schema, """SELECT w.warehouse_code AS warehouse, SUM(i.stock_value) AS stock_value
                                 FROM inventory i JOIN warehouses w ON w.warehouse_id = i.warehouse_id GROUP BY 1""")
        if not stock.empty:
            st.subheader("Stock value by warehouse")
            st.bar_chart(stock.set_index("warehouse").astype(float))


def _reports(code: str) -> list[Path]:
    folder = settings().data_dir / "reports" / code
    return sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True) if folder.exists() else []


def page_validation(selected) -> None:
    st.header("Validation")
    if selected is None:
        st.info("Pick a workspace in the sidebar.")
        return
    if st.button("Run all validation checks"):
        start_job(f"validate {selected.code}", ["validate", "-w", selected.code])
    reports = _reports(selected.code)
    if not reports:
        st.info("No validation report yet.")
    else:
        chosen = st.selectbox("Report", reports, format_func=lambda p: p.name)
        body = json.loads(chosen.read_text())
        results = pd.DataFrame(body["results"])
        counts = results["status"].value_counts().to_dict()
        cols = st.columns(5)
        for col, status in zip(cols, ("PASSED", "FAILED", "ERROR", "NOT_APPLICABLE", "PENDING")):
            col.metric(status.replace("_", " ").title(), counts.get(status, 0))
        shown = results[["rule_id", "module", "status", "violations", "reconciliation", "rule", "message"]]
        st.dataframe(shown, hide_index=True, width="stretch")
        failing = results[results["status"].isin(["FAILED", "ERROR"])]
        for _, row in failing.iterrows():
            with st.expander(f"{row['rule_id']} — {row['violations']} violation(s)"):
                st.json(row["sample"])
    job_panel(f"validate -w {selected.code}")


def page_scenarios(selected) -> None:
    st.header("Business scenarios (master prompt §6)")
    if selected is None:
        st.info("Pick a workspace in the sidebar.")
        return
    from scmgen.generation.scenarios import load_scenarios
    from scmgen.reports.scenarios import measure
    with engine(selected.oltp_schema).connect() as conn:
        rows = measure(conn, load_scenarios()["scenarios"])
    st.dataframe(pd.DataFrame([{
        "scenario": r.scenario, "measured": None if r.value is None else float(r.value),
        "band": f"{r.band[0]} – {r.band[1]}" + (" /month" if r.unit == "per_month" else ""), "status": r.status,
        "count": f"{r.numerator:,.0f} of {r.denominator:,.0f}", "settings": ", ".join(r.knobs)} for r in rows]),
        hide_index=True, width="stretch")
    st.caption("Bands and knobs: config/scenarios.yaml. A share outside its band names the generation settings to tune.")


def page_load(selected) -> None:
    st.header("DuckDB load")
    if selected is None:
        st.info("Pick a workspace in the sidebar.")
        return
    left, mid, right = st.columns(3)
    if left.button("Incremental load", type="primary"):
        start_job(f"load {selected.code}", ["load", "-w", selected.code])
    if mid.button("Full reload"):
        start_job(f"full load {selected.code}", ["load", "-w", selected.code, "--full"])
    table = right.text_input("Reload one table")
    if right.button("Reload table", disabled=not table):
        start_job(f"reload {table}", ["load", "-w", selected.code, "--table", table])
    from scmgen.loader.run import load_status
    try:
        status = load_status(settings(), selected.code)
    except Exception as exc:
        st.info(str(exc))
        job_panel(f"load -w {selected.code}")
        return
    st.caption(f"Replica {status['path']} — source build {'matches' if status['build_matches'] else 'CHANGED: next load is full'}"
               f" — high-water mark {status['info'].get('high_water_mark')}")
    st.dataframe(pd.DataFrame(status["tables"], columns=["table", "strategy", "watermark", "watermark pk", "rows",
                                                          "last load", "status", "reconciliation", "finished"]),
                 hide_index=True, width="stretch")
    if status["errors"]:
        st.subheader("Recent loader errors")
        st.dataframe(pd.DataFrame(status["errors"], columns=["when", "table", "stage", "type", "message"]),
                     hide_index=True, width="stretch")
    job_panel(f"load -w {selected.code}")


@st.cache_data
def table_docs():
    return load_table_docs()


@st.cache_data
def examples():
    return load_examples()


def page_guide(selected) -> None:
    st.header("Table guide")
    docs = table_docs()
    if selected is None:
        st.info("Pick a workspace in the sidebar to see live keys, relations and row counts.")
    names = sorted(n for n in docs if not n.startswith(("etl_", "v_")))
    modules = sorted({docs[n].module for n in names})
    module = st.selectbox("Module", ["(all)"] + modules)
    choices = [n for n in names if module == "(all)" or docs[n].module == module]
    table = st.selectbox("Table", choices, index=choices.index("orders") if "orders" in choices else 0)
    doc = docs[table]
    st.subheader(table)
    st.caption(f"Design: Part {doc.part} — {doc.module}")
    if doc.summary:
        st.markdown(doc.summary)
    parents, children, pk = [], [], None
    if selected is not None:
        schema = selected.oltp_schema
        fk = query(schema, """SELECT TABLE_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
                              FROM information_schema.KEY_COLUMN_USAGE
                              WHERE TABLE_SCHEMA = DATABASE() AND REFERENCED_TABLE_NAME IS NOT NULL""")
        parents = [(r.COLUMN_NAME, r.REFERENCED_TABLE_NAME, r.REFERENCED_COLUMN_NAME)
                   for r in fk[fk.TABLE_NAME == table].itertuples() if r.REFERENCED_TABLE_NAME != table]
        children = [(r.TABLE_NAME, r.COLUMN_NAME) for r in fk[fk.REFERENCED_TABLE_NAME == table].itertuples()
                    if r.TABLE_NAME != table]
        pk_rows = query(schema, """SELECT COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA = DATABASE()
                                   AND TABLE_NAME = :t AND CONSTRAINT_NAME = 'PRIMARY'""", t=table)
        pk = pk_rows["COLUMN_NAME"][0] if not pk_rows.empty else None
        rows = int(query(schema, f"SELECT COUNT(*) AS n FROM `{table}`")["n"][0])
        st.metric("Rows in this workspace", f"{rows:,}")
    tab_cols, tab_rel, tab_notes, tab_queries = st.tabs(["Columns", "Relations", "How it is populated", "Example queries"])
    with tab_cols:
        if doc.columns:
            st.dataframe(pd.DataFrame(doc.columns), hide_index=True, width="stretch")
        elif selected is not None:
            st.dataframe(query(selected.oltp_schema, """SELECT COLUMN_NAME AS `column`, COLUMN_TYPE AS type,
                                   IS_NULLABLE AS nullable, COLUMN_KEY AS `key` FROM information_schema.COLUMNS
                                   WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t ORDER BY ORDINAL_POSITION""", t=table),
                         hide_index=True, width="stretch")
    with tab_rel:
        if selected is None:
            st.info("Relations come from the live schema: pick a workspace.")
        else:
            st.graphviz_chart(relations_dot(table, parents, children))
            st.markdown("**References (parents):** " + (", ".join(f"`{c}` → `{p}`" for c, p, _ in parents) or "none"))
            st.markdown("**Referenced by (children):** " + (", ".join(f"`{t}.{c}`" for t, c in children) or "none"))
    with tab_notes:
        for note in doc.notes or ["(no notes in the design)"]:
            st.markdown(f"- {note}")
    with tab_queries:
        listed = examples().get(table, [])
        generated = {"title": "Join to the parent tables", "sql": join_example(table, pk or f"{table[:-1]}_id", parents)}
        for example in listed + [generated]:
            st.markdown(f"**{example['title']}**")
            st.code(example["sql"], language="sql")
            if selected is not None and st.button("Run on MySQL", key=f"run-{table}-{example['title']}"):
                sql = example["sql"].strip().rstrip(";")
                if not sql.lower().startswith("select"):
                    st.error("Only SELECT queries run here.")
                else:
                    st.dataframe(query(selected.oltp_schema, f"SELECT * FROM ({sql}) q LIMIT 500"), width="stretch")


def page_reset(selected) -> None:
    st.header("Reset")
    if selected is None:
        st.info("Pick a workspace in the sidebar.")
        return
    st.warning(f"Deleting **{selected.code}** drops its MySQL schemas and DuckDB replica. This can't be undone.")
    purge = st.checkbox("Also remove the registry entry (the code can be reused)")
    confirm = st.text_input("Type the workspace code to confirm")
    if st.button("Delete workspace", disabled=confirm != selected.code, type="primary"):
        start_job(f"delete {selected.code}", ["workspace", "delete", "-w", selected.code, "--yes"]
                  + (["--purge"] if purge else []))


# ------------------------------------------------------------------------------------------ main


def main() -> None:
    st.sidebar.title("scmgen")
    st.sidebar.caption("Synthetic supply-chain OLTP + DuckDB replica")
    rows = workspaces()
    codes = [w.code for w in rows]
    code = st.sidebar.selectbox("Workspace", ["(none)"] + codes, index=1 if codes else 0)
    selected = next((w for w in rows if w.code == code), None)
    if selected is not None:
        st.sidebar.markdown(f"**{selected.company_name}**  \nstatus: `{selected.status}`  \n"
                            f"committed through: {selected.last_committed_through or '-'}")
    page = st.sidebar.radio("Page", PAGES)
    {"Workspaces": page_workspaces, "Run monitor": page_monitor, "Data explorer": page_explorer,
     "Validation": page_validation, "Scenarios": page_scenarios, "DuckDB load": page_load,
     "Table guide": page_guide, "Reset": page_reset}[page](selected)


main()
