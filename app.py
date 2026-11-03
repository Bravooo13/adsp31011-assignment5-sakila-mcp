"""Run with streamlit run app.py. Business access always goes through MCP."""
import asyncio
from copy import deepcopy
from datetime import date
from decimal import Decimal
import json

import pandas as pd
import streamlit as st

from assistant import GROUP_KEYS, METRIC_LABELS, answer_question, readable_error
from planner import Scope

st.set_page_config(page_title="Ask Sakila", page_icon="🎬", layout="wide")
st.title("Ask Sakila")
st.caption("Explore cash collected, payments and paying customers in plain English.")

defaults = {"start_date": date(2005, 1, 1), "end_date": date(2006, 12, 31),
            "store": "All stores", "rating": "All ratings", "question": "", "answer": None}
for key, value in defaults.items():
    if key not in st.session_state:
        st.session_state[key] = value

with st.sidebar:
    st.header("Your filters")
    st.date_input("From", key="start_date", min_value=date(1900,1,1), max_value=date(2099,12,31))
    st.date_input("Through", key="end_date", min_value=date(1900,1,1), max_value=date(2099,12,31))
    st.selectbox("Store", ["All stores", "Store 1", "Store 2"], key="store")
    st.selectbox("Film rating", ["All ratings", "G", "PG", "PG-13", "R", "NC-17"], key="rating")
    st.caption("Questions use these filters. Ask about a narrower period or store within them.")
    st.divider()
    st.caption("Payment data covers cash collected. Unpaid rentals, profit and inventory availability need other data.")

with st.form("question_form"):
    st.text_input("What would you like to know?", key="question", max_chars=2000,
                  placeholder="How much cash did each store collect?")
    submitted = st.form_submit_button("Ask", type="primary")

if submitted:
    # Clear the previous answer before a new attempt so failure cannot show stale facts as new.
    st.session_state.answer = None
    if not st.session_state.question.strip():
        st.warning("Enter a question first.")
    else:
        try:
            scope = Scope(start_date=st.session_state.start_date, end_date=st.session_state.end_date,
                store_id={"All stores": None, "Store 1": 1, "Store 2": 2}[st.session_state.store],
                rating=None if st.session_state.rating == "All ratings" else st.session_state.rating)
            with st.spinner("Reading the schema, planning your question and checking Snowflake…"):
                st.session_state.answer = asyncio.run(answer_question(st.session_state.question.strip(), scope))
        except Exception as error:
            st.error(readable_error(error))


def show_cost(cost):
    history = cost.get("rows", [])
    if cost.get("history_status") == "available" and history:
        row = history[0]
        c1, c2, c3 = st.columns(3)
        c1.metric("Snowflake elapsed", f"{row['TOTAL_ELAPSED_TIME']:,} ms")
        credits = cost.get("cloud_services_credits")
        c2.metric("Cloud-services credits", str(credits) if credits is not None else "Unavailable")
        c3.metric("Bytes scanned", f"{row['BYTES_SCANNED']:,}")
    else:
        st.write("Query timing and cloud-services credits are pending or unavailable.")
    st.caption("Cloud-services credits are not total warehouse cost or a final bill. Missing cost is unknown, not zero.")
    if cost.get("compute_status") == "available":
        st.write(f"Attributed compute credits: {cost['compute_credits']} (excludes idle time).")


def chart_for(answer):
    payload = answer.get("chart")
    if not payload or not payload.get("rows"):
        return
    frame = pd.DataFrame(payload["rows"])
    group = answer["chart_group"]
    label = METRIC_LABELS[answer["chart_metric"]]
    st.subheader("Payment amounts" if group == "payments" else f"{label} by {group}")
    if group == "payments":
        frame["AMOUNT"] = pd.to_numeric(frame["AMOUNT"])
        frame["PAYMENT_ID"] = frame["PAYMENT_ID"].astype(str)
        frame = frame.rename(columns={"PAYMENT_ID": "Payment ID", "AMOUNT": "Payment amount"})
        st.bar_chart(frame, x="Payment ID", y="Payment amount", sort=False)
        st.caption("Amounts for the returned payment rows only; this is not the full-period total.")
    else:
        frame["VALUE"] = pd.to_numeric(frame["VALUE"])
        key = GROUP_KEYS[group]
        if group == "month":
            frame[key] = pd.to_datetime(frame[key]).dt.strftime("%Y-%m")
            frame = frame.rename(columns={key: "Month", "VALUE": label})
            st.bar_chart(frame, x="Month", y=label, sort=False)
            st.caption("Only months with matching payment data are shown. Missing months are not assumed to be zero.")
        else:
            frame[key] = frame[key].map(lambda value: "Unknown" if pd.isna(value) else str(value))
            group_label = group.title()
            frame = frame.rename(columns={key: group_label, "VALUE": label})
            st.bar_chart(frame, x=group_label, y=label, sort=False)
        if answer["chart_metric"] == "paying_customers":
            st.caption("A customer may appear in several groups. Do not add these counts to get an overall distinct total.")
    if payload.get("truncated"):
        st.warning("The chart contains only the returned groups or payments; additional rows were omitted.")


answer = st.session_state.answer
if answer is None:
    st.info("Try: “Show monthly revenue”, “How many distinct paying customers were there?”, or “Show 10 payment details”.")
else:
    current = {"start_date": st.session_state.start_date.isoformat(), "end_date": st.session_state.end_date.isoformat(),
        "store_id": {"All stores": None, "Store 1": 1, "Store 2": 2}[st.session_state.store],
        "rating": None if st.session_state.rating == "All ratings" else st.session_state.rating}
    if current != answer["selected_scope"]:
        st.warning("Filters have changed. This is the previous answer; press Ask to apply the new filters.")
    st.subheader(answer["question"])
    if answer["decision"] == "clarify":
        st.warning(answer["message"])
    elif answer["decision"] == "unsupported":
        st.info("This question cannot be answered by the available payment tools.")
        st.write(answer["message"])
    else:
        arguments = answer["arguments"]
        store_label = "All stores" if arguments["store_id"] is None else f"Store {arguments['store_id']}"
        rating_label = arguments["rating"] or "All ratings"
        st.caption(f"Applied: {arguments['start_date']} through {arguments['end_date']} · {store_label} · {rating_label}")
        st.write(answer["message"])
        if answer.get("headline"):
            st.metric(METRIC_LABELS[arguments["metric"]], answer["headline"])
        if answer["primary"].get("truncated"):
            st.warning("Only part of the result is shown. These rows must not be treated as a complete total or a top-spending ranking.")
        if answer["primary"].get("warning"):
            st.warning(answer["primary"]["warning"])
        if answer.get("chart_notice"):
            st.info(answer["chart_notice"])
        chart_for(answer)
        if answer["primary"].get("rows"):
            with st.expander("View answer data"):
                display = pd.DataFrame(answer["primary"]["rows"])
                display.columns = [column.replace("_", " ").title() for column in display.columns]
                st.dataframe(display, hide_index=True, width="stretch")

    with st.expander("Check the answer — tools, SQL, rows and cost"):
        if answer.get("interpretation"):
            st.write("AI interpretation: " + answer["interpretation"])
        for entry in answer["trace"]:
            result = entry["result"]
            st.markdown(f"**{entry['purpose']} · {result['tool']}**")
            st.write(f"Returned {result['row_count']:,} rows. {result['row_meaning']}")
            if result.get("truncated"):
                st.write("The result was truncated.")
            st.code(result["sql"], language="sql")
            if result.get("parameters"):
                st.table([{"Position": index + 1, "Bound value": str(value)} for index, value in enumerate(result["parameters"])])
            st.caption("Query ID: " + result["query_id"])
            show_cost(entry["cost"])
            cost = entry["cost"]
            if cost.get("sql"):
                st.write("Cost lookup: report_query_cost")
                st.code(cost["sql"], language="sql")
                st.caption(f"Cost lookup returned {cost['row_count']} rows. Its own execution is an additional query.")
            st.divider()
    export = deepcopy(answer)
    for entry in export["trace"]:
        cost = entry["cost"]
        if cost.get("parameters"):
            cost["parameters"][0] = "[session ID omitted]"
    st.download_button("Download this result", data=json.dumps(export, indent=2),
        file_name="sakila_answer.json", mime="application/json", on_click="ignore")
