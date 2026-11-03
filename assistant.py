"""Streamlit orchestration: AI plan -> real MCP calls -> evidence-based answer."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
import os
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from planner import Scope, choose_plan, plan_arguments

METRIC_LABELS = {"revenue": "Cash collected", "payment_count": "Payments",
                 "paying_customers": "Distinct paying customers", "average_payment": "Average payment"}
GROUP_KEYS = {"month": "PAYMENT_MONTH", "store": "STORE_ID", "customer": "CUSTOMER_ID",
              "film": "FILM_ID", "rating": "RATING"}


@asynccontextmanager
async def connect_mcp():
    parameters = StdioServerParameters(command=sys.executable,
        args=[str(Path(__file__).with_name("server.py"))], env=dict(os.environ))
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def call_tool(session, name: str, arguments: dict) -> dict:
    result = await session.call_tool(name, arguments)
    if result.is_error:
        raise RuntimeError(f"The {name} tool could not complete. Check the Snowflake connection and try again.")
    if not isinstance(result.structured_content, dict):
        raise RuntimeError("The MCP server returned an unreadable result. No answer was inferred.")
    return result.structured_content


async def evidence_for(session, payload: dict, purpose: str) -> dict:
    try:
        cost = await call_tool(session, "report_query_cost", {"query_id": payload["query_id"]})
    except RuntimeError:
        cost = {"history_status": "unavailable", "cloud_services_credits": None,
                "compute_credits": None, "compute_status": "unavailable"}
    return {"purpose": purpose, "result": payload, "cost": cost}


def make_answer(result: dict, arguments: dict) -> tuple[str, str | None]:
    """Numbers come only from returned rows; model prose cannot supply facts."""
    if result.get("status") == "no_data" or not result.get("rows"):
        return "No payment data matched this question and the applied filters. This does not establish that the answer is zero.", None
    if result["tool"] == "query_payments":
        text = f"Returned {result['row_count']:,} individual payments. Each row is one payment, not one rental or customer."
        return text, None
    metric = arguments["metric"]
    if arguments["group_by"] != "none":
        text = f"{METRIC_LABELS[metric]} by {arguments['group_by']}, with {result['row_count']:,} groups returned."
        return text, None
    row = result["rows"][0]
    value = Decimal(str(row["VALUE"]))
    payments = int(row["MATCHING_PAYMENT_ROWS"])
    display = f"${value:,.2f}" if metric in ("revenue", "average_payment") else f"{int(value):,}"
    if metric == "revenue":
        text = f"Cash collected was {display} across {payments:,} payment rows."
    elif metric == "paying_customers":
        text = f"{display} distinct paying customers appear in {payments:,} payment rows. This counts customers with payments, not all renters."
    elif metric == "payment_count":
        text = f"There were {display} payment rows. This is not a rental count or a distinct customer count."
    else:
        text = f"The average payment was {display}, based on {payments:,} payment rows. This is not average spending per customer."
    return text, display


async def answer_question(question: str, scope: Scope) -> dict:
    async with connect_mcp() as session:
        tools = (await session.list_tools()).tools
        expected = {"aggregate_payments", "describe_schema", "query_payments", "report_query_cost"}
        if {tool.name for tool in tools} != expected:
            raise RuntimeError("The MCP server's tool list differs from the reviewed Part 1 server.")
        schema = await call_tool(session, "describe_schema", {})
        trace = [await evidence_for(session, schema, "Schema check")]
        descriptions = [{"name": tool.name, "description": tool.description,
                         "input_schema": tool.input_schema} for tool in tools]
        plan = await asyncio.to_thread(choose_plan, question, scope, descriptions, schema)
        response = {"question": question, "selected_scope": scope.model_dump(mode="json"),
            "decision": plan.decision, "message": plan.message, "trace": trace,
            "created_at": datetime.now(timezone.utc).isoformat(), "model_plan": plan.model_dump(mode="json")}
        if plan.decision != "execute":
            return response
        try:
            arguments = plan_arguments(plan, scope)
        except ValueError as error:
            response.update(decision="clarify", message=str(error))
            return response
        primary = await call_tool(session, plan.tool, arguments)
        trace.append(await evidence_for(session, primary, "Answer"))
        answer, headline = make_answer(primary, arguments)
        response.update(message=answer, arguments=arguments, primary=primary, headline=headline,
                        interpretation=plan.message)
        if primary.get("rows"):
            if plan.tool == "aggregate_payments" and plan.group_by == "none":
                chart_args = {**arguments, "group_by": "month", "limit": 500}
                try:
                    chart = await call_tool(session, "aggregate_payments", chart_args)
                    trace.append(await evidence_for(session, chart, "Monthly chart"))
                    response.update(chart=chart, chart_group="month", chart_metric=plan.metric)
                except RuntimeError:
                    response["chart_notice"] = "The answer query succeeded, but the monthly chart could not be loaded."
            else:
                response.update(chart=primary, chart_group=plan.group_by if plan.tool == "aggregate_payments" else "payments",
                                chart_metric=plan.metric if plan.tool == "aggregate_payments" else "revenue")
        return response


def readable_error(error: Exception) -> str:
    if isinstance(error, BaseExceptionGroup):
        return " ".join(readable_error(child) for child in error.exceptions if isinstance(child, Exception))
    if isinstance(error, (RuntimeError, ValueError)):
        return str(error)
    return "The request could not be completed. Check Cloud Shell's Snowflake and Claude connections, then try again."
