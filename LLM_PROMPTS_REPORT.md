# LLM Prompts Report

This report lists every live LLM prompt flow found in the project.

Scope:
- Searched for actual model invocation sites such as `chat.completions.create`
- Confirmed all active LLM calls are centralized in `backend/services/llm_service.py`
- Excluded non-LLM helper text such as `llm_summary` in preprocessing because it is not sent to a model

Model used:
- `llama-3.3-70b-versatile` in [backend/services/llm_service.py](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/services/llm_service.py:22)

## Summary

| # | Function | Purpose | Output Type |
|---|---|---|---|
| 1 | `SemanticLayer._resolve_via_llm()` | Map business words to real column names | JSON object |
| 2 | `generate_sql()` | Convert user question into DuckDB SQL | SQL string |
| 3 | `review_sql()` | Verify whether generated SQL matches user intent | JSON object |
| 4 | `repair_sql()` | Repair invalid or failed SQL | SQL string |
| 5 | `explain_result()` | Explain results and generate insights | Structured text with sections |
| 6 | `suggest_questions()` | Generate 4 suggested questions for a dataset | JSON array of strings |

## 1. Semantic Column Mapping

Source:
- [backend/services/llm_service.py:214](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/services/llm_service.py:214)

Task:
- Resolves user business terms such as `"revenue"` or `"customer"` to actual dataset column names before SQL generation.

System prompt:

```text
You are a precise data dictionary assistant. Return only a JSON object, nothing else.
```

User prompt template:

```text
You are a data dictionary expert.

Table columns:
{schema_lines}

Sample rows:
{sample_str}

Business terms to map: {terms_json}

For each business term, find the SINGLE best matching column name from the table above. If there is no reasonable match, omit that term.

Return ONLY a valid JSON object mapping each resolved term to its column name. No markdown, no explanation. Example:
{"revenue": "amount", "customer": "user_id"}
```

Expected output:

```json
{
  "revenue": "amount",
  "customer": "user_id"
}
```

How output is used:
- Parsed with `json.loads`
- Filtered so only real column names survive
- Appended back into the user question as semantic hints

Failure behavior:
- If parsing or model call fails, returns `{}` and continues without semantic enrichment

## 2. Natural Language to SQL

Source:
- [backend/services/llm_service.py:274](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/services/llm_service.py:274)

Task:
- Converts the user question into a DuckDB-compatible read-only SQL query on table `data`.

System prompt:

```text
You are an expert DuckDB SQL analyst.
RULES:
1. The table name is ALWAYS 'data' — never use any other table name.
2. Return ONLY the SQL query — no markdown, no explanation, no preamble.
3. Always quote column names with double-quotes to handle spaces/special chars.
4. Use DuckDB-compatible syntax (e.g. PERCENTILE_CONT for percentiles).
5. For comparative questions (e.g. 'versus', 'compare'), ensure you GROUP BY the relevant column to show a comparison.
6. If the user asks for a plot, graph, or chart, ensure the query returns at least one categorical/date column and one numeric column for visualization.
7. Keep the query concise and efficient.
8. NEVER use DROP, DELETE, INSERT, UPDATE, ALTER, CREATE or any DDL/DML that modifies data. Only SELECT statements are allowed.

Table schema:
{schema_lines}

Sample rows (first 5):
{sample_str}
```

User prompt template:

```text
Question: {enriched_question}
```

Expected output:

```sql
SELECT "region", SUM("sales") AS "total_sales"
FROM data
GROUP BY "region"
ORDER BY "total_sales" DESC
```

How output is used:
- Cleaned with `_clean_sql()`
- Validated by `validate_sql()` to ensure it is read-only
- Sent to DuckDB for execution

Notes:
- Conversation history may be appended before the current user question
- In `smart` mode, the prompt input schema/sample may already reflect preprocessed column names and cleaned sample rows
- In `raw` and currently `scalable` mode, prompt input starts from the raw schema/sample

Failure behavior:
- Raises if SQL is unsafe or generation fails

## 3. SQL Review / Guardian Verification

Source:
- [backend/services/llm_service.py:366](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/services/llm_service.py:366)

Task:
- Reviews candidate SQL and checks whether it actually answers the user question.

System prompt:

```text
You verify SQL correctness with strict reasoning. Return only valid JSON.
```

User prompt template:

```text
You are a strict SQL verifier for DuckDB queries.

User question:
{question}

Candidate SQL:
{sql}

Table schema:
{schema_lines}

Sample rows:
{sample_str}

Task:
1. Decide if SQL answers the user's intent.
2. Check if selected/grouped columns and filters are logically correct.
3. If wrong, provide one corrected SQL query for DuckDB.

Return ONLY a JSON object in this format:
{"verdict":"PASS|FAIL","reason":"short reason","fixed_sql":"corrected query or empty"}
```

Expected output:

```json
{
  "verdict": "PASS",
  "reason": "The SQL matches the requested grouping and metric.",
  "fixed_sql": ""
}
```

or

```json
{
  "verdict": "FAIL",
  "reason": "The query aggregates the wrong metric.",
  "fixed_sql": "SELECT ..."
}
```

How output is used:
- Parsed as JSON
- `verdict` must be `PASS` or `FAIL`
- `fixed_sql` is cleaned and validated before use

Failure behavior:
- On model/parsing failure, defaults to a permissive pass:

```json
{
  "verdict": "PASS",
  "reason": "Verifier unavailable; continuing with generated SQL.",
  "fixed_sql": ""
}
```

## 4. SQL Repair

Source:
- [backend/services/llm_service.py:462](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/services/llm_service.py:462)

Task:
- Generates a corrected SQL query after validation failure, semantic mismatch, or runtime error.

System prompt:

```text
You are an expert DuckDB SQL repair assistant.
RULES:
1. Table name is always data.
2. Return only SQL.
3. Use only read-only SELECT/CTE queries.
4. Quote column names using double-quotes.
5. Keep the query concise and correct.

Table schema:
{schema_lines}

Sample rows:
{sample_str}
```

User prompt template:

```text
Original question: {question}

Failed SQL:
{failed_sql}

Failure reason:
{error_reason}

Return a corrected DuckDB SQL query that answers the original question.
```

Expected output:

```sql
SELECT "department", AVG("salary") AS "avg_salary"
FROM data
GROUP BY "department"
```

How output is used:
- Cleaned with `_clean_sql()`
- Validated with `validate_sql()`
- Reused by the SQL guardian / query pipeline

Notes:
- Up to 4 previous chat history messages may also be included before the current repair request

Failure behavior:
- Raises if the repaired SQL is invalid or unsafe

## 5. Result Explanation and Insights

Source:
- [backend/services/llm_service.py:774](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/services/llm_service.py:774)

Task:
- Turns query results into a human-readable explanation, insights list, and likely reasons behind the observed pattern.

System prompt:

```text
You are a concise, insightful data analyst. Always follow the exact section format. Ground every claim in the provided rows and never invent metrics.
```

User prompt template:

```text
The user asked: "{question}"
SQL executed: {sql}
Result columns: {cols_line}
Result preview (up to 20 rows):
{result_preview}

Provide the following — use EXACTLY the section headers shown:

EXPLANATION: <2-4 sentences in plain English summarising the key finding and scale>

INSIGHTS:
• <insight 1 with concrete value/metric>
• <insight 2 with concrete value/metric>
• <insight 3 (trend, spread, or anomaly)>
• <insight 4 if applicable>

WHY: <1-2 sentences on likely drivers rooted in the observed numbers>
```

Expected output:

```text
EXPLANATION: Revenue is highest in the North region, which contributes the largest share of total sales in the returned result set.

INSIGHTS:
• North generated 42% of the total shown revenue.
• South ranked second with 28%.
• The gap between first and second place is substantial.

WHY: The result suggests stronger concentration of sales in the North region relative to the others returned by the query.
```

How output is used:
- Parsed by `_parse_explanation()`
- Split into:
  - `explanation`
  - `insights`
  - `why_analysis`

Failure behavior:
- If the model is weak or fails, falls back to deterministic explanation logic

## 6. Suggested Questions

Source:
- [backend/services/llm_service.py:846](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/services/llm_service.py:846)

Task:
- Generates exactly 4 starter questions after a CSV upload so the UI can suggest useful analyses.

System prompt:

```text
You are a data analyst suggesting insightful questions about a dataset.
```

User prompt template:

```text
Schema:
{schema_lines}

Sample:
{sample_str}

Suggest exactly 4 interesting, specific questions a business analyst would want to ask about this dataset.
Return ONLY a JSON array of 4 strings. No other text.
```

Expected output:

```json
[
  "Which region has the highest total sales?",
  "How do average sales vary by category?",
  "Are there any trends over time in revenue?",
  "Which customer segment contributes the most revenue?"
]
```

How output is used:
- Parsed as JSON
- Truncated to first 4 items
- Returned from upload flow for UI suggestions

Failure behavior:
- Falls back to four hard-coded generic questions

## Where These Prompts Are Used

Upload flow:
- [backend/routers/upload.py:50](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/routers/upload.py:50) uses `suggest_questions()`

Query flow:
- [backend/routers/query.py:71](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/routers/query.py:71) uses `generate_sql()`
- [backend/routers/query.py:232](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/routers/query.py:232) uses `explain_result()`
- [backend/routers/query.py:394](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/routers/query.py:394) uses `review_sql()`
- [backend/routers/query.py:589](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/routers/query.py:589) uses `repair_sql()`

## Final Finding

There are 6 live LLM prompt flows in the project, and all of them are currently implemented in one file:
- [backend/services/llm_service.py](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/services/llm_service.py:1)
