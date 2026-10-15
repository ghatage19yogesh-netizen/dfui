"""
SKU Exclusion Manager — Streamlit version of the Dynamic Fulfillment Flask app.

Run (dummy data, default):
    streamlit run streamlit_app.py

Run against BigQuery (prod needs explicit CLI flag, like the Flask app):
    streamlit run streamlit_app.py -- --backend bigquery --env dev
    streamlit run streamlit_app.py -- --backend bigquery --env prod
"""
import argparse
import os
import re
import sys
from datetime import datetime
from decimal import Decimal

import pandas as pd
import inspect

import streamlit as st

# Full-width kwarg that works on both new (width="stretch") and older Streamlit
_NEW_WIDTH = "width" in inspect.signature(st.button).parameters
WIDE = {"width": "stretch"} if _NEW_WIDTH else {"use_container_width": True}

# --------------------------------------------------------------------------- #
# Config (same as app.py)
# --------------------------------------------------------------------------- #
PROCESS_CONFIG = {
    "cs_margin": {
        "id": "cs_margin",
        "process_name": "CS Margin",
        "table_name": "cs_margin_excluded_skus",
        "label": "cs margin (cs_margin_excluded_skus)",
    },
    "ctf": {
        "id": "ctf",
        "process_name": "Sellout Risk",
        "table_name": "ctf_excluded_skus",
        "label": "ctf (ctf_excluded_skus)",
    },
    "replen": {
        "id": "replen",
        "process_name": "Replen",
        "table_name": "rplnsh_excluded_skus",
        "label": "replen (rplnsh_excluded_skus)",
    },
    "vdf_margin": {
        "id": "vdf_margin",
        "process_name": "VDF Margin",
        "table_name": "vdf_margin_excluded_skus",
        "label": "vdf margin (vdf_margin_excluded_skus)",
    },
}

DATASETS = {
    "bigquery": {
        "prod": "mtech-daas-appdata.ewphoc",
        "dev": "mcy-eda-anlytcs-sbox.dynamic_fulfillment_dev",
    },
    "demo": {
        "prod": "demo-project.df_prod",
        "dev": "demo-project.df_dev",
    },
}

DEMO_SEED = {
    "cs_margin": [100000000001, 100000000002, 100000000003, 100000000004, 100000000005],
    "ctf": [200000000001, 200000000002, 200000000003],
    "replen": [300000000001, 300000000002, 300000000003, 300000000004],
    "vdf_margin": [400000000001, 400000000002],
}


# --------------------------------------------------------------------------- #
# CLI args (streamlit run streamlit_app.py -- --env prod --backend bigquery)
# --------------------------------------------------------------------------- #
def parse_cli():
    parser = argparse.ArgumentParser(description="SKU Exclusion Manager")
    parser.add_argument("--env", "--run-env", "--run_env", "-e", dest="run_env", default=None,
                        help="Target environment ('prod' or 'dev'). Overrides RUN_ENV env var.")
    parser.add_argument("--backend", choices=["demo", "bigquery"], default="demo",
                        help="'demo' = in-memory dummy data (default), 'bigquery' = real tables.")
    args, _ = parser.parse_known_args(sys.argv[1:])
    return args


CLI = parse_cli()
CLI_ENV = (CLI.run_env or "").strip().lower() or None
DEFAULT_ENV = CLI_ENV or os.getenv("RUN_ENV", "dev").strip().lower()
BACKEND = CLI.backend


# --------------------------------------------------------------------------- #
# Pure helpers (same logic as app.py)
# --------------------------------------------------------------------------- #
def parse_skus(sku_text: str) -> list[int]:
    """Extracts unique integer SKUs from comma, newline, or space-delimited text."""
    tokens = re.split(r"[\s,]+", sku_text.strip())
    skus = [int(t) for t in tokens if t.isdigit()]
    return list(dict.fromkeys(skus))


def preview_counts(sku_text: str) -> dict:
    tokens = [t for t in re.split(r"[\s,]+", sku_text.strip()) if t]
    valid = [int(t) for t in tokens if t.isdigit()]
    unique = list(dict.fromkeys(valid))
    return {"unique": len(unique), "dup": len(valid) - len(unique), "bad": len(tokens) - len(valid)}


def get_process_info(process_key: str | None) -> dict:
    if not process_key or not str(process_key).strip():
        raise ValueError("Please select a target process/table.")
    key = str(process_key).strip().lower()
    for item in PROCESS_CONFIG.values():
        if key in (item["id"], item["process_name"].lower(), item["table_name"].lower()):
            return item
    valid = [f"'{p['process_name']}' / '{p['table_name']}'" for p in PROCESS_CONFIG.values()]
    raise ValueError(f"Invalid process or table '{process_key}'. Valid choices: {', '.join(valid)}")


# --------------------------------------------------------------------------- #
# Backends
# --------------------------------------------------------------------------- #
class DemoBackend:
    """In-memory tables held in session_state. Nothing leaves the app."""

    def __init__(self, env: str):
        st.session_state.setdefault("demo_db", {})
        db = st.session_state.demo_db
        if env not in db:
            db[env] = {k: set(v) for k, v in DEMO_SEED.items()}
        self.tables = db[env]
        st.session_state.setdefault("demo_job_n", 1)

    def _table(self, proc):
        return self.tables[proc["id"]]

    def get_existing_skus(self, skus, proc, target_table):
        t = self._table(proc)
        return {s for s in skus if s in t}

    def execute(self, action, skus, proc, target_table):
        t = self._table(proc)
        if action == "add":
            new = [s for s in skus if s not in t]
            t.update(new)
            n = len(new)
        else:
            n = sum(1 for s in skus if s in t)
            t.difference_update(skus)
        job_id = f"demo_job_{st.session_state.demo_job_n:04d}"
        st.session_state.demo_job_n += 1
        return job_id, n

    def snapshot(self):
        return {k: sorted(v) for k, v in self.tables.items()}


class BigQueryBackend:
    def __init__(self, env: str):
        from google.cloud import bigquery  # imported only when used
        self.bq = bigquery
        self.client = _bq_client()

    def _params(self, skus):
        return self.bq.QueryJobConfig(
            query_parameters=[self.bq.ArrayQueryParameter("skus", "NUMERIC", [Decimal(s) for s in skus])]
        )

    def get_existing_skus(self, skus, proc, target_table):
        if not skus:
            return set()
        query = f"""
            SELECT sku_upc_nbr
            FROM `{target_table}`
            WHERE sku_upc_nbr IN UNNEST(@skus)
        """
        job = self.client.query(query, job_config=self._params(skus))
        return {int(r["sku_upc_nbr"]) for r in job.result() if r["sku_upc_nbr"] is not None}

    def execute(self, action, skus, proc, target_table):
        if action == "add":
            query = f"""
                INSERT INTO `{target_table}` (dept_nbr, sku_upc_nbr)
                SELECT 0, sku
                FROM UNNEST(@skus) AS sku
            """
        else:
            query = f"""
                DELETE FROM `{target_table}`
                WHERE sku_upc_nbr IN UNNEST(@skus)
            """
        job = self.client.query(query, job_config=self._params(skus))
        job.result()
        n = job.num_dml_affected_rows if job.num_dml_affected_rows is not None else len(skus)
        return job.job_id, n

    def snapshot(self):
        return None


@st.cache_resource
def _bq_client():
    from google.cloud import bigquery
    return bigquery.Client()


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #
st.set_page_config(page_title="SKU Exclusion Manager", page_icon="📦", layout="wide")
st.session_state.setdefault("history", [])
st.session_state.setdefault("check", None)
st.session_state.setdefault("last_result", None)

# ---- Sidebar: environment ------------------------------------------------- #
with st.sidebar:
    st.header("Environment")
    if BACKEND == "demo":
        st.info("**Demo mode** — dummy in-memory tables. No BigQuery calls.")
        run_env = st.radio("Environment", ["dev", "prod"], index=1 if DEFAULT_ENV == "prod" else 0,
                           horizontal=True, help="Simulated — both are dummy data.", key="run_env")
    else:
        # Mirrors Flask: prod only when explicitly passed on the CLI
        run_env = DEFAULT_ENV if DEFAULT_ENV in ("dev", "prod") else "dev"
        st.write(f"Backend: **BigQuery**")
        st.write(f"Environment: **{run_env}** (set via `--env` / `RUN_ENV`)")

    is_prod = run_env == "prod"
    is_prod_cli_override = BACKEND == "bigquery" and is_prod and CLI_ENV == "prod"
    dataset = DATASETS[BACKEND][run_env]
    st.caption("Dataset")
    st.code(dataset, language=None)

    st.subheader("Exclusion tables")
    for p in PROCESS_CONFIG.values():
        st.caption(f"{p['process_name']} → `{p['table_name']}`")

backend = DemoBackend(run_env) if BACKEND == "demo" else BigQueryBackend(run_env)

# ---- Header ---------------------------------------------------------------- #
st.title("📦 SKU Exclusion Manager")
st.caption("Dynamic Fulfillment · add or remove SKUs from process exclusion tables")
if BACKEND == "demo":
    st.caption("🧪 DEMO MODE — dummy data only")
if is_prod:
    msg = "⚠️ You are writing to **PRODUCTION**"
    if is_prod_cli_override:
        msg += " (enabled via CLI flag override)"
    elif BACKEND == "demo":
        msg += " (simulated)"
    st.error(msg + ". Changes are applied immediately.")

left, right = st.columns(2, gap="large")

# ---- Inputs ---------------------------------------------------------------- #
with left:
    st.subheader("1 · Target process")
    proc_ids = sorted(PROCESS_CONFIG, key=lambda k: PROCESS_CONFIG[k]["process_name"].lower())
    process_key = st.selectbox(
        "Process",
        options=proc_ids,
        index=None,
        placeholder="Select a process / table…",
        format_func=lambda k: f"{PROCESS_CONFIG[k]['process_name']}  ·  {PROCESS_CONFIG[k]['table_name']}",
        label_visibility="collapsed",
    )
    if process_key:
        st.caption(f"Target: `{dataset}.{PROCESS_CONFIG[process_key]['table_name']}`")

    st.subheader("2 · Action")
    ACTIONS = {"➕ Add exclusions": "add", "➖ Remove exclusions": "remove"}
    action = ACTIONS[st.radio("Action", list(ACTIONS), horizontal=True, label_visibility="collapsed", key="action")]
    st.caption("New SKUs will be inserted." if action == "add" else "Matching SKUs will be deleted.")

    st.subheader("3 · SKU / UPC numbers")
    sku_text = st.text_area(
        "SKUs",
        height=200,
        placeholder="Paste SKUs separated by commas, spaces or new lines",
        label_visibility="collapsed",
        key="sku_text",
    )
    c = preview_counts(sku_text)
    m1, m2, m3 = st.columns(3)
    m1.metric("Unique valid", c["unique"])
    m2.metric("Duplicates", c["dup"])
    m3.metric("Ignored (non-numeric)", c["bad"])

    validate = st.button("Validate against table", type="primary",
                         disabled=not process_key or c["unique"] == 0, **WIDE)

# Signature of inputs: a check is only valid for the exact inputs it was run on
signature = (BACKEND, run_env, process_key, action, sku_text)

if validate:
    st.session_state.last_result = None
    try:
        proc = get_process_info(process_key)
        target_table = f"{dataset}.{proc['table_name']}"
        skus = parse_skus(sku_text)
        if not skus:
            raise ValueError("No valid numeric UPCs found in input.")
        with st.spinner("Checking table…"):
            existing = backend.get_existing_skus(skus, proc, target_table)
        present = [s for s in skus if s in existing]
        absent = [s for s in skus if s not in existing]
        ready, conflicts = (absent, present) if action == "add" else (present, absent)
        if conflicts:
            verb = "already exist in" if action == "add" else "were not found in"
            conflict_message = f"{len(conflicts)} SKU(s) {verb} {proc['process_name']} ({proc['table_name']})."
        else:
            conflict_message = ""
        st.session_state.check = {
            "signature": signature,
            "action": action,
            "proc": proc,
            "target_table": target_table,
            "total_parsed": len(skus),
            "ready": ready,
            "conflicts": conflicts,
            "conflict_message": conflict_message,
        }
    except ValueError as e:
        st.session_state.check = {"signature": signature, "error": str(e)}
    except Exception as e:  # BigQuery errors
        st.session_state.check = {"signature": signature, "error": f"BigQuery error: {e}"}

# ---- Results --------------------------------------------------------------- #
with right:
    st.subheader("4 · Review & execute")
    chk = st.session_state.check
    result = st.session_state.last_result

    if result:
        st.success(result["message"])
        st.caption(f"Job: `{result['job_id']}` · rows affected: {result['rows']}")
        if st.button("Start a new run"):
            st.session_state.last_result = None
            st.session_state.check = None
            st.rerun()
    elif not chk:
        st.info("Pick a process, paste SKUs and validate. Nothing is written until you execute.")
    elif chk["signature"] != signature:
        st.warning("Inputs changed — validate again before executing.")
    elif "error" in chk:
        st.error(chk["error"])
    else:
        is_add = chk["action"] == "add"
        ready, conflicts = chk["ready"], chk["conflicts"]
        ready_label = "Will be inserted" if is_add else "Will be deleted"
        skip_label = "Already excluded (skipped)" if is_add else "Not in table (skipped)"

        s1, s2, s3 = st.columns(3)
        s1.metric("Parsed", chk["total_parsed"])
        s2.metric(ready_label, len(ready))
        s3.metric("Skipped", len(conflicts))

        if chk["conflict_message"]:
            st.warning(chk["conflict_message"])

        t1, t2 = st.tabs([f"{ready_label} ({len(ready)})", f"{skip_label} ({len(conflicts)})"])
        for tab, data, name in ((t1, ready, "ready"), (t2, conflicts, "skipped")):
            with tab:
                if data:
                    df = pd.DataFrame({"sku_upc_nbr": [str(s) for s in data]})
                    st.dataframe(df, hide_index=True, **WIDE, height=min(35 * len(df) + 38, 240))
                    st.download_button("Download CSV", df.to_csv(index=False), f"{name}_skus.csv",
                                       "text/csv", key=f"dl_{name}")
                else:
                    st.caption("None")

        st.divider()
        st.caption(f"Target: `{chk['target_table']}`")

        if not ready:
            st.warning(f"Nothing to {'insert' if is_add else 'delete'} — all SKUs were skipped.")
        else:
            confirm_ok = True
            if is_prod:
                typed = st.text_input("Type **PROD** to confirm", key="prod_confirm")
                confirm_ok = typed.strip() == "PROD"
            ack = st.checkbox(
                f"I confirm: {'insert' if is_add else 'delete'} {len(ready)} SKU(s) in {chk['target_table']}",
                key="ack",
            )
            label = f"{'Insert' if is_add else 'Delete'} {len(ready)} SKU(s) in {chk['proc']['process_name']}"
            if st.button(label, type="primary", disabled=not (confirm_ok and ack), **WIDE):
                proc = chk["proc"]
                try:
                    with st.spinner("Executing…"):
                        job_id, n = backend.execute(chk["action"], ready, proc, chk["target_table"])
                    verb = "inserted" if is_add else "deleted"
                    prep = "into" if is_add else "from"
                    message = f"Successfully {verb} {n} SKU(s) {prep} {proc['process_name']} ({proc['table_name']})."
                    st.session_state.last_result = {"message": message, "job_id": job_id, "rows": n}
                    st.session_state.history.insert(0, {
                        "Time": datetime.now().strftime("%H:%M:%S"),
                        "Env": run_env,
                        "Action": "Add" if is_add else "Remove",
                        "Process": proc["process_name"],
                        "Rows": n,
                        "Job ID": job_id,
                    })
                    st.session_state.check = None
                    for k in ("prod_confirm", "ack"):
                        st.session_state.pop(k, None)
                    st.rerun()
                except Exception as e:
                    st.error(f"BigQuery error during execution: {e}")

# ---- History & demo tables -------------------------------------------------- #
if st.session_state.history:
    st.subheader("Session history")
    st.dataframe(pd.DataFrame(st.session_state.history), hide_index=True, **WIDE)

snap = backend.snapshot()
if snap is not None:
    with st.expander(f"Dummy tables — live contents ({run_env})", expanded=True):
        st.dataframe(
            pd.DataFrame([
                {
                    "Process": PROCESS_CONFIG[k]["process_name"],
                    "Table": PROCESS_CONFIG[k]["table_name"],
                    "Rows": len(v),
                    "SKUs": ", ".join(str(s) for s in v),
                }
                for k, v in snap.items()
            ]),
            hide_index=True,
            **WIDE,
        )
        st.caption("Try: pick CS Margin and paste `100000000001, 100000000009 abc 100000000009`")
        if st.button("Reset dummy data"):
            st.session_state.demo_db.pop(run_env, None)
            st.session_state.check = None
            st.session_state.last_result = None
            st.rerun()
