"""Bounded Sakila reads. No credentials or connections are loaded at import time."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
import os
from pathlib import Path
import re
from threading import RLock
from time import perf_counter
import tomllib
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


Group = Literal["none", "month", "store", "customer", "film", "rating"]
Metric = Literal["revenue", "payment_count", "paying_customers", "average_payment"]
Rating = Literal["G", "PG", "PG-13", "R", "NC-17"]
RowLimit = Annotated[int, Field(strict=True, ge=1, le=500)]
Store = Annotated[int, Field(strict=True, ge=1, le=2)]


class Filters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_date: date
    end_date: date
    store_id: Store | None = None
    rating: Rating | None = None

    @model_validator(mode="after")
    def check_period(self):
        if self.start_date > self.end_date:
            raise ValueError("start_date must be on or before end_date")
        if self.end_date == date.max:
            raise ValueError("end_date must be earlier than 9999-12-31")
        return self


class PaymentRequest(Filters):
    limit: RowLimit = 100


class AggregateRequest(Filters):
    metric: Metric = "revenue"
    group_by: Group = "none"
    limit: RowLimit = 100


FROM_PAYMENT = """FROM SAKILA.DW.FACT_PAYMENT f
JOIN SAKILA.DW.DIM_CUSTOMER c ON c.CUSTOMER_KEY = f.CUSTOMER_KEY
JOIN SAKILA.DW.DIM_FILM d ON d.FILM_KEY = f.FILM_KEY"""
GROUPS = {
    "none": None,
    "month": ("DATE_TRUNC('MONTH', f.PAYMENT_DATE)::DATE", "PAYMENT_MONTH"),
    "store": ("f.STORE_KEY", "STORE_ID"),
    "customer": ("c.CUSTOMER_ID", "CUSTOMER_ID"),
    "film": ("d.FILM_ID", "FILM_ID"),
    "rating": ("d.RATING", "RATING"),
}
METRICS = {
    "revenue": ("SUM(f.AMOUNT)", "Cash collected in payment amounts; not rental rates or profit."),
    "payment_count": ("COUNT(*)", "Number of payment rows; not rentals or distinct customers."),
    "paying_customers": ("COUNT(DISTINCT c.CUSTOMER_ID)", "Distinct customers with a payment in the selected period; not all renters."),
    "average_payment": ("AVG(f.AMOUNT)", "Average amount per payment; not average spending per customer."),
}
LIMITATIONS = [
    "Coverage is payment events, including payments without a rental. Unpaid rentals and inventory availability cannot be answered.",
    "Customer and film joins use the exact version key stored on each payment, not the current dimension version.",
    "Initial dimension history was assumed; later versions record observation time.",
    "Store means inventory store with staff-store fallback, as established in Assignment 4.",
    "Film category is not in the DW star. Film rating is supported; category is not interchangeable with rating.",
]
GRAINS = {
    "FACT_PAYMENT": "One payment ID, including payments without a rental.",
    "DIM_CUSTOMER": "One observed version of a customer; CUSTOMER_ID can repeat across versions.",
    "DIM_FILM": "One observed version of a film, plus unknown film key 0.",
    "DIM_DATE": "One calendar day observed in payment data, not a complete calendar.",
    "DIM_STORE": "One store, with Type 1 location attributes.",
    "CHECK_BASELINE": "One payment month/store/customer normalized-check aggregate for the latest refresh.",
    "LOAD_RUN": "One successful warehouse refresh.",
    "SCD_DEMO_STATE": "One controlled SCD2 demonstration marker.",
    "V_CUSTOMER_SOURCE": "One customer with flattened source address attributes.",
    "V_FILM_SOURCE": "One source film and its tracked commercial terms.",
    "V_NORMALIZED_PAYMENT": "One source payment with rental/film/store attribution.",
}


@dataclass(frozen=True)
class Statement:
    sql: str
    parameters: tuple
    row_meaning: str
    limit: int


def where(filters: Filters) -> tuple[str, tuple]:
    clauses = ["f.PAYMENT_DATE >= %s", "f.PAYMENT_DATE < %s"]
    # Date-only API uses inclusive start/end calendar dates, including the last day's times.
    values: list = [filters.start_date, filters.end_date + timedelta(days=1)]
    if filters.store_id is not None:
        clauses.append("f.STORE_KEY = %s")
        values.append(filters.store_id)
    if filters.rating is not None:
        clauses.append("d.RATING = %s")
        values.append(filters.rating)
    return "WHERE " + " AND ".join(clauses), tuple(values)


def payment_statement(request: PaymentRequest) -> Statement:
    predicate, parameters = where(request)
    sql = f"""SELECT f.PAYMENT_ID, f.PAYMENT_DATE, f.AMOUNT,
f.STORE_KEY AS STORE_ID, c.CUSTOMER_ID, d.FILM_ID, d.TITLE, d.RATING,
f.RENTAL_ID
{FROM_PAYMENT}
{predicate}
ORDER BY f.PAYMENT_DATE, f.PAYMENT_ID
LIMIT %s"""
    return Statement(sql, parameters + (request.limit + 1,), "One payment, not one rental or customer.", request.limit)


def aggregate_statement(request: AggregateRequest) -> Statement:
    predicate, parameters = where(request)
    expression, _ = METRICS[request.metric]
    group = GROUPS[request.group_by]
    prefix = f"{group[0]} AS {group[1]},\n" if group else ""
    grouping = f"\nGROUP BY {group[0]}" if group else ""
    ordering = f"\nORDER BY {group[1]} ASC NULLS LAST" if group else ""
    # HAVING avoids a misleading synthetic zero-count row for a period with no data.
    sql = f"""SELECT {prefix}{expression} AS VALUE, COUNT(*) AS MATCHING_PAYMENT_ROWS
{FROM_PAYMENT}
{predicate}{grouping}
HAVING COUNT(*) > 0{ordering}
LIMIT %s"""
    grain = "One aggregate for the selected period." if group is None else f"One {request.group_by} group for the selected period."
    return Statement(sql, parameters + (request.limit + 1,), grain, request.limit)


SCHEMA_SQL = """SELECT t.TABLE_NAME, t.TABLE_TYPE, t.COMMENT AS TABLE_COMMENT,
c.COLUMN_NAME, c.DATA_TYPE, c.NUMERIC_PRECISION, c.NUMERIC_SCALE,
c.IS_NULLABLE, c.ORDINAL_POSITION
FROM SAKILA.INFORMATION_SCHEMA.TABLES t
JOIN SAKILA.INFORMATION_SCHEMA.COLUMNS c
  ON c.TABLE_CATALOG = t.TABLE_CATALOG AND c.TABLE_SCHEMA = t.TABLE_SCHEMA
  AND c.TABLE_NAME = t.TABLE_NAME
WHERE t.TABLE_SCHEMA = 'DW'
ORDER BY t.TABLE_NAME, c.ORDINAL_POSITION
LIMIT 501"""
HISTORY_SQL = """SELECT QUERY_ID, TOTAL_ELAPSED_TIME, EXECUTION_TIME, COMPILATION_TIME,
BYTES_SCANNED, ROWS_PRODUCED, CREDITS_USED_CLOUD_SERVICES, EXECUTION_STATUS
FROM TABLE(SAKILA.INFORMATION_SCHEMA.QUERY_HISTORY_BY_SESSION(
  SESSION_ID => %s,
  END_TIME_RANGE_START => DATEADD('hour', -24, CURRENT_TIMESTAMP()),
  RESULT_LIMIT => 10000, INCLUDE_CLIENT_GENERATED_STATEMENT => TRUE))
WHERE QUERY_ID = %s
LIMIT 1"""
ATTRIBUTION_SQL = """SELECT CREDITS_ATTRIBUTED_COMPUTE, CREDITS_USED_QUERY_ACCELERATION
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY
WHERE QUERY_ID = %s AND START_TIME >= DATEADD('day', -1, CURRENT_TIMESTAMP())
LIMIT 1"""


def json_value(value):
    if isinstance(value, Decimal):
        return str(value)  # Preserve exact money; the UI may convert for chart rendering.
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: json_value(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(v) for v in value]
    return value


def connection_settings() -> dict:
    """Reuse the private Cloud Shell CLI connection; never copy it to the project."""
    name = os.getenv("SAKILA_CONNECTION", "claude")
    root = Path(os.getenv("SNOWFLAKE_HOME", "~/.snowflake")).expanduser()
    explicit = os.getenv("SAKILA_CONFIG_FILE")
    path = Path(explicit).expanduser() if explicit else root / "connections.toml"
    if not path.exists() and not explicit:
        path = root / "config.toml"
    if not path.is_file():
        raise RuntimeError("Snowflake connection file not found. Run this in Cloud Shell where the Assignment 4 claude connection is configured.")
    if path.stat().st_mode & 0o077:
        raise RuntimeError("Restrict your Snowflake connection file to owner-only access (chmod 600) before running.")
    try:
        document = tomllib.loads(path.read_text())
        entries = document.get("connections", document)
        config = entries[name]
        keys = ("account", "user", "authenticator", "private_key_file", "private_key_file_pwd", "private_key_password")
        settings = {k: config[k] for k in keys if k in config}
        if not all(settings.get(k) for k in ("account", "user", "private_key_file")):
            raise ValueError("missing key configuration")
        settings["private_key_file"] = str(Path(settings["private_key_file"]).expanduser())
    except (KeyError, TypeError, ValueError):
        raise RuntimeError("Named Snowflake connection needs account, user and private_key_file. Check it privately; do not paste credentials into chat.") from None
    settings.update(authenticator="SNOWFLAKE_JWT", role="CLAUDE_RO", warehouse="CLASS_WH",
                    database="SAKILA", schema="DW", login_timeout=20, network_timeout=60,
                    session_parameters={"QUERY_TAG": "ADSP31011_A5_MCP", "STATEMENT_TIMEOUT_IN_SECONDS": 30})
    return settings


class Warehouse:
    """Serialize reads on one session and remember only the last 500 issued query IDs."""
    def __init__(self):
        self._connection = None
        self._lock = RLock()
        self._issued: OrderedDict[str, dict] = OrderedDict()

    def close(self):
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def _connect(self):
        if self._connection is None or self._connection.is_closed():
            import snowflake.connector
            try:
                self._connection = snowflake.connector.connect(**connection_settings())
            except RuntimeError:
                raise
            except Exception:
                raise RuntimeError("Snowflake login failed. Check the existing claude key connection privately; no credentials are needed in chat.") from None
        return self._connection

    def _select(self, statement: Statement, tool: str, remember=True) -> dict:
        with self._lock:
            connection = self._connect()
            started = perf_counter()
            try:
                with connection.cursor() as cursor:
                    cursor.execute(statement.sql, statement.parameters)
                    columns = [item[0] for item in cursor.description]
                    raw = cursor.fetchmany(statement.limit + 1)
                    query_id = cursor.sfqid
            except Exception as exc:
                code = getattr(exc, "errno", None)
                raise RuntimeError(f"Snowflake read failed (code {code or 'unavailable'}). Check role access and table definitions in Snowflake.") from None
            result = {
                "tool": tool, "sql": statement.sql,
                "parameters": json_value(statement.parameters), "query_id": query_id,
                "row_count": min(len(raw), statement.limit),
                "row_meaning": statement.row_meaning,
                "truncated": len(raw) > statement.limit,
                "client_elapsed_ms": round((perf_counter() - started) * 1000, 3),
                "rows": json_value([dict(zip(columns, row)) for row in raw[:statement.limit]]),
            }
            if remember:
                self._issued[query_id] = {"session_id": connection.session_id,
                                           "client_elapsed_ms": result["client_elapsed_ms"]}
                while len(self._issued) > 500:
                    self._issued.popitem(last=False)
            return result

    def describe_schema(self) -> dict:
        result = self._select(Statement(SCHEMA_SQL, (), "One column of a DW table or view.", 500), "describe_schema")
        objects = {}
        for row in result["rows"]:
            name = row["TABLE_NAME"]
            item = objects.setdefault(name, {"name": name, "type": row["TABLE_TYPE"],
                "comment": row["TABLE_COMMENT"], "row_meaning": GRAINS.get(name, "Unknown; inspect its definition before use."), "columns": []})
            item["columns"].append({key: row[key] for key in ("COLUMN_NAME", "DATA_TYPE", "NUMERIC_PRECISION", "NUMERIC_SCALE", "IS_NULLABLE")})
        result.update(objects=list(objects.values()), limitations=LIMITATIONS,
            relationships=[f"FACT_PAYMENT.{key} = {table}.{key}" for key, table in
                           [("CUSTOMER_KEY", "DIM_CUSTOMER"), ("FILM_KEY", "DIM_FILM"),
                            ("DATE_KEY", "DIM_DATE"), ("STORE_KEY", "DIM_STORE")]])
        return result

    def query_payments(self, **arguments) -> dict:
        request = PaymentRequest(**arguments)
        result = self._select(payment_statement(request), "query_payments")
        result.update(filters=request.model_dump(mode="json"), limitations=LIMITATIONS,
                      status="ok" if result["rows"] else "no_data")
        if not result["rows"]:
            result["message"] = "No payment data matched these filters; this is not evidence that the answer is zero."
        return result

    def aggregate_payments(self, **arguments) -> dict:
        request = AggregateRequest(**arguments)
        result = self._select(aggregate_statement(request), "aggregate_payments")
        result.update(filters=request.model_dump(mode="json"), metric=request.metric,
            metric_meaning=METRICS[request.metric][1], limitations=LIMITATIONS,
            status="ok" if result["rows"] else "no_data")
        if not result["rows"]:
            result["message"] = "No payment data matched these filters; this is not evidence that the answer is zero."
        if request.metric == "paying_customers" and request.group_by != "none":
            result["warning"] = "Distinct customers can appear in multiple groups. Do not sum group counts to obtain an overall distinct customer count."
        return result

    def report_query_cost(self, query_id: str) -> dict:
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", query_id):
            raise ValueError("query_id must be a Snowflake query ID returned by this server")
        with self._lock:
            original = self._issued.get(query_id)
        if original is None:
            raise ValueError("Only the last 500 queries issued in this server process can be inspected. Run a tool again after restarting.")
        result = self._select(Statement(HISTORY_SQL, (original["session_id"], query_id),
            "One QUERY_HISTORY record for the requested query, if available.", 1), "report_query_cost", remember=False)
        result.update(target_query_id=query_id, history_status="available" if result["rows"] else "pending_or_not_visible",
            cloud_services_credits=result["rows"][0]["CREDITS_USED_CLOUD_SERVICES"] if result["rows"] else None,
            compute_credits=None, query_acceleration_credits=None, compute_status="not_requested",
            credit_scope="QUERY_HISTORY credits are cloud-services usage only, not total warehouse cost or a final bill.",
            target_client_elapsed_ms=original["client_elapsed_ms"])
        if os.getenv("SAKILA_INCLUDE_COMPUTE_CREDITS") == "1":
            try:
                attribution = self._select(Statement(ATTRIBUTION_SQL, (query_id,),
                    "One compute-credit attribution record, if published and visible.", 1), "report_query_cost", remember=False)
                result["attribution_evidence"] = attribution
                if attribution["rows"]:
                    row = attribution["rows"][0]
                    result.update(compute_credits=row["CREDITS_ATTRIBUTED_COMPUTE"],
                        query_acceleration_credits=row["CREDITS_USED_QUERY_ACCELERATION"], compute_status="available")
                else:
                    result["compute_status"] = "pending_or_not_attributed"
            except RuntimeError:
                result["compute_status"] = "unavailable"
            result["compute_note"] = "Attribution can lag up to eight hours and omit short queries. It excludes warehouse idle time and other costs. Missing credits are unknown, not zero."
        return result
