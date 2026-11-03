# ADSP31011 Assignment 5

A Streamlit app and four-tool MCP server for the existing Sakila Snowflake warehouse.

## Run

Use the Cloud Shell environment from Assignment 4 with Python 3.11 or newer, Claude Code signed in, and the existing Snowflake key connection named `claude`.

Clone this private repository, then run:

```bash
git clone https://github.com/Bravooo13/adsp31011-assignment5-sakila-mcp.git
cd adsp31011-assignment5-sakila-mcp
bash run_app.sh
```

Keep the terminal running and open **Web Preview -> Preview on port 8080**. The script installs the dependencies in a local virtual environment and starts Streamlit. If Claude requests a new sign-in, run `claude auth login` in another terminal and ask again.

The Snowflake connection is read privately from `~/.snowflake/connections.toml` (`[claude]`) or `~/.snowflake/config.toml` (`[connections.claude]`). It must include the account, user and private-key-file path and have owner-only permissions. The app uses role `CLAUDE_RO`, warehouse `CLASS_WH`, database `SAKILA` and schema `DW`. Credentials and keys stay outside this submission. `SAKILA_CONFIG_FILE` and `SAKILA_CONNECTION` can override the connection file/name.

## MCP tools

- `describe_schema`: tables, columns, relationships and row meanings.
- `query_payments`: payment details for validated dates, store and film rating.
- `aggregate_payments`: revenue, payment counts, distinct paying customers or average payment.
- `report_query_cost`: elapsed time and cloud-services credits from query history.

## Why four bounded tools?

I used four tools to describe the schema, retrieve payment details, calculate aggregates, and check query cost. This gives the app a small set of actions that I can test and explain. A tool that accepts any SQL would let the model choose its own tables, joins and calculations, making errors harder to catch. My tools validate dates, bind filter values, allow only named metrics and grouping choices, and return no more than 500 rows. They also distinguish payment rows from distinct customers. The connection uses the read-only CLAUDE_RO role. For each answer, I can show the selected tool, SQL, inputs, row count, row meaning and query cost.

## App behavior

Claude selects a supported tool and its inputs. Python validates the plan, calls the tool through MCP, and displays the returned answer and chart. Date, store and rating filters keep their values across reruns. The Check expander shows SQL, bound values, row counts, row meanings and cost.

Revenue is cash collected, not profit. Empty results mean no matching data, not zero revenue. Unsupported questions are declined. Cloud-services credits are not full warehouse cost. Questions are standalone; the app does not remember conversational follow-ups. The five challenge tests and the remaining automatic date-coverage limitation are described in the separate PDF.

## Assistance disclosure

I used Codex to help build the Streamlit app. I used Claude to interpret questions and check whether the available tools could answer them.
