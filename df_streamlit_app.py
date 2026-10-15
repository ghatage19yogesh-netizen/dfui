"""
Product Exclusion Tool — business-friendly front end for the Dynamic Fulfillment
SKU exclusion tables.

Run (practice mode, dummy data — default):
    streamlit run business_app.py

Run against BigQuery:
    streamlit run business_app.py -- --backend bigquery --env dev
    streamlit run business_app.py -- --backend bigquery --env prod
"""
import argparse
import inspect
import io
import os
import re
import sys
from datetime import datetime
from decimal import Decimal

import pandas as pd
import streamlit as st

_NEW_WIDTH = "width" in inspect.signature(st.button).parameters
WIDE = {"width": "stretch"} if _NEW_WIDTH else {"use_container_width": True}

# --------------------------------------------------------------------------- #
# Business-facing configuration — edit descriptions to match your teams' words
# --------------------------------------------------------------------------- #
PROCESS_CONFIG = {
    "cs_margin": {
        "name": "CS Margin",
        "table_name": "cs_margin_excluded_skus",
        "description": "Products left out of the CS margin calculation.",
    },
    "ctf": {
        "name": "Sellout Risk",
        "table_name": "ctf_excluded_skus",
        "description": "Products left out of sell-out risk checks.",
    },
    "replen": {
        "name": "Replenishment",
        "table_name": "rplnsh_excluded_skus",
        "description": "Products left out of automatic replenishment.",
    },
    "vdf_margin": {
        "name": "VDF Margin",
        "table_name": "vdf_margin_excluded_skus",
        "description": "Products left out of the VDF margin calculation.",
    },
}

DATASETS = {
    "bigquery": {"prod": "mtech-daas-appdata.ewphoc", "dev": "mcy-eda-anlytcs-sbox.dynamic_fulfillment_dev"},
    "demo": {"prod": "demo-project.df_prod", "dev": "demo-project.df_dev"},
}

DEMO_SEED = {
    "cs_margin": [100000000001, 100000000002, 100000000003, 100000000004, 100000000005],
    "ctf": [100000000001, 200000000002, 200000000003],
    "replen": [300000000001, 300000000002, 100000000003, 300000000004],
    "vdf_margin": [400000000001, 100000000002],
}

ENV_LABEL = {"dev": "Test", "prod": "Live"}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _cli():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", "--run-env", "--run_env", "-e", dest="run_env", default=None)
    ap.add_argument("--backend", choices=["demo", "bigquery"], default="demo")
    a, _ = ap.parse_known_args(sys.argv[1:])
    return a


CLI = _cli()
BACKEND = CLI.backend
CLI_ENV = (CLI.run_env or "").strip().lower() or None
DEFAULT_ENV = CLI_ENV or os.getenv("RUN_ENV", "dev").strip().lower()
if DEFAULT_ENV not in ("dev", "prod"):
    DEFAULT_ENV = "dev"


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def split_entries(text: str):
    """Returns (unique valid product numbers in order, duplicates count, invalid entries)."""
    tokens = [t for t in re.split(r"[\s,;]+", text.strip()) if t]
    valid, invalid = [], []
    for t in tokens:
        t2 = t[:-2] if t.endswith(".0") else t  # Excel often turns 123 into 123.0
        (valid if t2.isdigit() else invalid).append(t2 if t2.isdigit() else t)
    nums = [int(v) for v in valid]
    unique = list(dict.fromkeys(nums))
    return unique, len(nums) - len(unique), list(dict.fromkeys(invalid))


def read_upload(file) -> str:
    name = file.name.lower()
    if name.endswith((".xlsx", ".xls")):
        df = pd.read_excel(file, header=None, dtype=str)
    else:
        df = pd.read_csv(io.StringIO(file.getvalue().decode("utf-8", "ignore")), header=None, dtype=str)
    col = df.iloc[:, 0].dropna().astype(str)
    return "\n".join(col)


# --------------------------------------------------------------------------- #
# Data backends
# --------------------------------------------------------------------------- #
class DemoBackend:
    def __init__(self, env):
        db = st.session_state.setdefault("demo_db", {})
        if env not in db:
            db[env] = {k: set(v) for k, v in DEMO_SEED.items()}
        self.tables = db[env]
        st.session_state.setdefault("demo_job_n", 1)

    def existing(self, skus, pid, full_table):
        return {s for s in skus if s in self.tables[pid]}

    def apply(self, action, skus, pid, full_table):
        t = self.tables[pid]
        if action == "add":
            new = [s for s in skus if s not in t]
            t.update(new)
            n = len(new)
        else:
            n = sum(1 for s in skus if s in t)
            t.difference_update(skus)
        ref = f"PRACTICE-{st.session_state.demo_job_n:04d}"
        st.session_state.demo_job_n += 1
        return ref, n


@st.cache_resource
def _bq_client():
    from google.cloud import bigquery
    return bigquery.Client()


class BigQueryBackend:
    def __init__(self, env):
        from google.cloud import bigquery
        self.bq = bigquery
        self.client = _bq_client()

    def _cfg(self, skus):
        return self.bq.QueryJobConfig(query_parameters=[
            self.bq.ArrayQueryParameter("skus", "NUMERIC", [Decimal(s) for s in skus])])

    def existing(self, skus, pid, full_table):
        if not skus:
            return set()
        q = f"SELECT sku_upc_nbr FROM `{full_table}` WHERE sku_upc_nbr IN UNNEST(@skus)"
        return {int(r["sku_upc_nbr"]) for r in self.client.query(q, job_config=self._cfg(skus)).result()
                if r["sku_upc_nbr"] is not None}

    def apply(self, action, skus, pid, full_table):
        if action == "add":
            q = (f"INSERT INTO `{full_table}` (dept_nbr, sku_upc_nbr) "
                 f"SELECT 0, sku FROM UNNEST(@skus) AS sku")
        else:
            q = f"DELETE FROM `{full_table}` WHERE sku_upc_nbr IN UNNEST(@skus)"
        job = self.client.query(q, job_config=self._cfg(skus))
        job.result()
        n = job.num_dml_affected_rows if job.num_dml_affected_rows is not None else len(skus)
        return job.job_id, n


# --------------------------------------------------------------------------- #
# Page setup & styling
# --------------------------------------------------------------------------- #
st.set_page_config(page_title="Product Exclusion Tool", page_icon="🛒", layout="centered")
st.markdown("""
<style>
  .block-container { padding-top: 2rem; max-width: 860px; }
  .step-title { font-size: 1.15rem; font-weight: 600; margin: 1.4rem 0 .3rem; }
  .step-num { display:inline-block; width:1.7rem; height:1.7rem; line-height:1.7rem; text-align:center;
              border-radius:50%; background:#1f4e79; color:#fff; font-size:.9rem; margin-right:.5rem; }
  .summary-card { border-radius: 12px; padding: 1rem 1.2rem; margin: .5rem 0; font-size: 1.02rem; }
  .card-go   { background:#e7f5ec; border-left: 6px solid #1f7a4d; }
  .card-skip { background:#eef2f7; border-left: 6px solid #6b7a90; }
  .card-bad  { background:#fff4e0; border-left: 6px solid #b26a00; }
  .big { font-size: 1.6rem; font-weight: 700; margin-right: .4rem; }
  .env-live { background:#b42318; color:#fff; padding:.15rem .6rem; border-radius:999px; font-weight:600; font-size:.8rem; }
  .env-test { background:#e8eefb; color:#2b59c3; padding:.15rem .6rem; border-radius:999px; font-weight:600; font-size:.8rem; }
</style>
""", unsafe_allow_html=True)

for k, v in {"log": [], "review": None, "done": None}.items():
    st.session_state.setdefault(k, v)


def step(n, text):
    st.markdown(f'<div class="step-title"><span class="step-num">{n}</span>{text}</div>', unsafe_allow_html=True)


def plural(n, word="product"):
    return f"{n} {word}{'' if n == 1 else 's'}"


# --------------------------------------------------------------------------- #
# Sidebar
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.markdown("### Settings")
    if BACKEND == "demo":
        st.success("**Practice mode**\n\nNothing you do here changes real data. Feel free to try things out.")
        env = st.radio("Which system?", ["dev", "prod"], format_func=lambda e: ENV_LABEL[e],
                       index=1 if DEFAULT_ENV == "prod" else 0, key="env",
                       help="Test = safe sandbox. Live = affects real fulfillment.")
    else:
        env = DEFAULT_ENV
        st.write(f"System: **{ENV_LABEL[env]}**")
    dataset = DATASETS[BACKEND][env]

    st.markdown("### How it works")
    st.markdown(
        "1. Choose whether to **exclude** products or **bring them back**.\n"
        "2. Pick the **process** it applies to.\n"
        "3. Paste product numbers or upload a file.\n"
        "4. **Review** the plain-English summary.\n"
        "5. **Confirm** — the change applies straight away."
    )
    st.caption("Questions? Contact the Dynamic Fulfillment team.")

backend = DemoBackend(env) if BACKEND == "demo" else BigQueryBackend(env)
full_table = lambda pid: f"{dataset}.{PROCESS_CONFIG[pid]['table_name']}"

# --------------------------------------------------------------------------- #
# Header
# --------------------------------------------------------------------------- #
badge = '<span class="env-live">LIVE SYSTEM</span>' if env == "prod" else '<span class="env-test">TEST SYSTEM</span>'
st.markdown(f"## 🛒 Product Exclusion Tool &nbsp;{badge}", unsafe_allow_html=True)
st.write("Stop specific products from being picked up by a fulfillment process, or bring them back.")
if env == "prod":
    st.error("You're working on the **Live** system. Changes take effect immediately.", icon="⚠️")

tab_change, tab_check, tab_log = st.tabs(["✏️ Make a change", "🔍 Check a product", "📋 Activity log"])

# --------------------------------------------------------------------------- #
# TAB 1 — Make a change
# --------------------------------------------------------------------------- #
def change_tab():
    done = st.session_state.done
    if done:
        if not done.get("shown"):
            st.balloons()
        done["shown"] = True
        st.success(f"### ✅ Done\n{done['message']}")
        st.caption(f"Reference: {done['ref']}  ·  {done['time']}")
        if st.button("Make another change", type="primary"):
            st.session_state.done = None
            st.session_state.review = None
            for k in ("entries", "ack"):
                st.session_state.pop(k, None)
            st.rerun()
        return

    step(1, "What would you like to do?")
    ACTIONS = {
        "Exclude products": "add",
        "Bring products back": "remove",
    }
    action_label = st.radio(
        "action", list(ACTIONS), key="action", label_visibility="collapsed", horizontal=True,
        captions=["The process will ignore these products.", "The process will include these products again."],
    )
    action = ACTIONS[action_label]

    step(2, "Which process does this apply to?")
    pids = list(PROCESS_CONFIG)
    pid = st.radio(
        "process", pids, index=None, key="process", label_visibility="collapsed",
        format_func=lambda p: PROCESS_CONFIG[p]["name"],
        captions=[PROCESS_CONFIG[p]["description"] for p in pids],
    )

    step(3, "Which products?")
    src = st.radio("source", ["Type or paste numbers", "Upload a file"], horizontal=True,
                   key="src", label_visibility="collapsed")
    if src == "Upload a file":
        up = st.file_uploader("Excel or CSV — product numbers in the first column", type=["xlsx", "xls", "csv"])
        text = ""
        if up is not None:
            try:
                text = read_upload(up)
            except Exception as e:
                st.error(f"Couldn't read that file. Please check it's a normal Excel or CSV file. ({e})")
    else:
        text = st.text_area("Product numbers", key="entries", height=150, label_visibility="collapsed",
                            placeholder="Paste product numbers (UPCs) here — one per line, or separated by commas")

    skus, dups, invalid = split_entries(text)
    if text.strip():
        bits = [f"**{plural(len(skus))}** found"]
        if dups:
            bits.append(f"{dups} repeated (counted once)")
        if invalid:
            bits.append(f"{len(invalid)} not recognised")
        st.caption(" · ".join(bits))

    signature = (env, action, pid, text)
    can_review = bool(pid and skus)
    if st.button("Review my request →", type="primary", disabled=not can_review, **WIDE):
        try:
            with st.spinner("Checking the current list…"):
                existing = backend.existing(skus, pid, full_table(pid))
            already = [s for s in skus if s in existing]
            missing = [s for s in skus if s not in existing]
            go, skip = (missing, already) if action == "add" else (already, missing)
            st.session_state.review = dict(signature=signature, action=action, pid=pid,
                                           go=go, skip=skip, invalid=invalid)
            st.session_state.pop("ack", None)
        except Exception as e:
            st.session_state.review = dict(signature=signature, error=str(e))
    if not can_review:
        st.caption("Choose a process and add at least one product number to continue.")

    rv = st.session_state.review
    if rv and rv["signature"] == signature:
        step(4, "Review")
        if "error" in rv:
            st.error("We couldn't reach the data right now. Please try again, or contact the support team.")
            with st.expander("Details for the support team"):
                st.code(rv["error"])
        else:
            pname = PROCESS_CONFIG[rv["pid"]]["name"]
            is_add = rv["action"] == "add"
            go, skip, bad = rv["go"], rv["skip"], rv["invalid"]

            if go:
                verb = f"will be <b>excluded from {pname}</b>" if is_add else f"will be <b>brought back into {pname}</b>"
                st.markdown(f'<div class="summary-card card-go"><span class="big">{len(go)}</span>'
                            f'{"product" if len(go) == 1 else "products"} {verb}.</div>', unsafe_allow_html=True)
            if skip:
                why = f"already excluded from {pname}" if is_add else f"not currently excluded from {pname}"
                st.markdown(f'<div class="summary-card card-skip"><span class="big">{len(skip)}</span>'
                            f'{"is" if len(skip) == 1 else "are"} {why} — nothing to do.</div>', unsafe_allow_html=True)
            if bad:
                st.markdown(f'<div class="summary-card card-bad"><span class="big">{len(bad)}</span>'
                            f'{"entry was" if len(bad) == 1 else "entries were"} not a valid product number '
                            f'and will be ignored.</div>', unsafe_allow_html=True)

            rows = ([{"Product": str(s), "What will happen": "Exclude" if is_add else "Bring back"} for s in go] +
                    [{"Product": str(s), "What will happen": "No change (already done)"} for s in skip] +
                    [{"Product": s, "What will happen": "Ignored (not a number)"} for s in bad])
            with st.expander(f"See the full list ({len(rows)})"):
                df = pd.DataFrame(rows)
                st.dataframe(df, hide_index=True, **WIDE)
                st.download_button("Download list", df.to_csv(index=False), "review.csv", "text/csv")

            if go:
                step(5, "Confirm")
                ack_text = (f"I want to {'exclude' if is_add else 'bring back'} {plural(len(go))} "
                            f"{'from' if is_add else 'into'} {pname}")
                if env == "prod":
                    ack_text += " on the **Live** system"
                ack = st.checkbox(ack_text, key="ack")
                btn = f"{'Exclude' if is_add else 'Bring back'} {plural(len(go))}"
                if st.button(btn, type="primary", disabled=not ack, **WIDE):
                    try:
                        with st.spinner("Applying your change…"):
                            ref, n = backend.apply(rv["action"], go, rv["pid"], full_table(rv["pid"]))
                        msg = (f"{plural(n)} {'excluded from' if is_add else 'brought back into'} {pname}.")
                        now = datetime.now().strftime("%d %b %Y, %H:%M")
                        st.session_state.done = dict(message=msg, ref=ref, time=now)
                        st.session_state.log.insert(0, {
                            "When": now, "System": ENV_LABEL[env],
                            "Change": "Excluded" if is_add else "Brought back",
                            "Process": pname, "Products": n, "Reference": ref,
                            "Product numbers": ", ".join(map(str, go)),
                        })
                        st.session_state.review = None
                        st.rerun()
                    except Exception as e:
                        st.error("Something went wrong and the change was not applied. Please try again.")
                        with st.expander("Details for the support team"):
                            st.code(str(e))
            else:
                st.info("There's nothing to change — every product is already in the state you asked for.")
    elif rv:
        st.caption("You changed something above — click **Review my request** again.")

with tab_change:
    change_tab()

# --------------------------------------------------------------------------- #
# TAB 2 — Check a product
# --------------------------------------------------------------------------- #
with tab_check:
    st.write("See which processes a product is currently excluded from.")
    q = st.text_area("Product numbers to check", key="lookup", height=100,
                     placeholder="One or more product numbers")
    lskus, _, lbad = split_entries(q)
    if st.button("Check", disabled=not lskus):
        try:
            with st.spinner("Looking up…"):
                found = {pid: backend.existing(lskus, pid, full_table(pid)) for pid in PROCESS_CONFIG}
            table = []
            for s in lskus:
                row = {"Product": str(s)}
                for pid, cfg in PROCESS_CONFIG.items():
                    row[cfg["name"]] = "🚫 Excluded" if s in found[pid] else "✅ Included"
                table.append(row)
            st.dataframe(pd.DataFrame(table), hide_index=True, **WIDE)
            st.caption("🚫 Excluded = the process ignores this product.  ✅ Included = normal.")
        except Exception as e:
            st.error("We couldn't look that up right now. Please try again.")
            with st.expander("Details for the support team"):
                st.code(str(e))
    if lbad:
        st.caption(f"Ignored (not a number): {', '.join(lbad)}")

# --------------------------------------------------------------------------- #
# TAB 3 — Activity log
# --------------------------------------------------------------------------- #
with tab_log:
    if not st.session_state.log:
        st.info("Changes you make in this session will appear here.")
    else:
        df = pd.DataFrame(st.session_state.log)
        st.dataframe(df.drop(columns=["Product numbers"]), hide_index=True, **WIDE)
        st.download_button("Download activity log", df.to_csv(index=False), "activity_log.csv", "text/csv")

# --------------------------------------------------------------------------- #
# Technical details (hidden by default)
# --------------------------------------------------------------------------- #
with st.expander("Technical details"):
    st.caption(f"Data source: {'dummy in-memory tables' if BACKEND == 'demo' else 'BigQuery'}  ·  dataset `{dataset}`")
    st.dataframe(pd.DataFrame([{"Process": c["name"], "Table": c["table_name"]} for c in PROCESS_CONFIG.values()]),
                 hide_index=True, **WIDE)
    if BACKEND == "demo" and st.button("Reset practice data"):
        st.session_state.demo_db.pop(env, None)
        st.session_state.review = None
        st.rerun()
