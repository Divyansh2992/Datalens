# LLM Complete Report

This document combines:
- the full inventory of every live LLM prompt in the project
- what each prompt does
- the prompt text/template
- expected output format
- where each prompt is used
- how prompts behave across `raw`, `smart`, and `scalable` modes

## 1. Executive Summary

### Main finding

The project does **not** currently have 3 separate LLM prompt systems for `raw`, `smart`, and `scalable`.

Instead, it has:
- 1 configured LLM model
- 6 live LLM prompt flows
- shared prompt templates across modes
- different preprocessing and execution paths by mode

### Configured model

Source:
- [backend/services/llm_service.py](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/services/llm_service.py:22)

Model:
- `llama-3.3-70b-versatile`

### Where all live LLM calls live

All active LLM calls are centralized in:
- [backend/services/llm_service.py](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/services/llm_service.py:1)

## 2. Live LLM Prompt Inventory

| # | Function | Task | Output Type |
|---|---|---|---|
| 1 | `SemanticLayer._resolve_via_llm()` | Map business terms to dataset columns | JSON object |
| 2 | `generate_sql()` | Convert question to DuckDB SQL | SQL string |
| 3 | `review_sql()` | Verify generated SQL matches intent | JSON object |
| 4 | `repair_sql()` | Repair broken or misaligned SQL | SQL string |
| 5 | `explain_result()` | Explain result and generate insights | Structured text |
| 6 | `suggest_questions()` | Generate 4 suggested questions on upload | JSON array |

## 3. Prompt-by-Prompt Details

### 3.1 Semantic Column Mapping

Source:
- [backend/services/llm_service.py:214](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/services/llm_service.py:214)

Task:
- Resolve business words like `"revenue"` or `"customer"` into real column names before SQL generation.

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

How it is used:
- Parsed as JSON
- Invalid target columns are discarded
- Valid mappings are injected back into the user question as semantic hints

Failure behavior:
- Returns `{}` if the call or parsing fails

### 3.2 Natural Language to SQL

Source:
- [backend/services/llm_service.py:274](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/services/llm_service.py:274)

Task:
- Convert a user question into a read-only DuckDB SQL query on table `data`.

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

How it is used:
- Cleaned with `_clean_sql()`
- Validated with `validate_sql()`
- Executed against DuckDB

Notes:
- Up to 6 history messages may be included
- `smart` mode may feed cleaned schema/sample into this prompt

Failure behavior:
- Raises if generation fails or SQL is unsafe

### 3.3 SQL Review / Guardian Verification

Source:
- [backend/services/llm_service.py:366](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/services/llm_service.py:366)

Task:
- Decide whether candidate SQL actually answers the user question and optionally provide a corrected query.

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

How it is used:
- Parsed as JSON
- `fixed_sql` is cleaned and validated before reuse

Failure behavior:
- Falls back to a permissive pass result

### 3.4 SQL Repair

Source:
- [backend/services/llm_service.py:462](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/services/llm_service.py:462)

Task:
- Produce corrected SQL after validation, semantic, or runtime failure.

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

How it is used:
- Cleaned and revalidated
- Reused by the SQL guardian or query pipeline

Notes:
- Up to 4 history messages may be included

Failure behavior:
- Raises if the repaired SQL is invalid or unsafe

### 3.5 Result Explanation and Insights

Source:
- [backend/services/llm_service.py:774](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/services/llm_service.py:774)

Task:
- Turn query results into a plain-English explanation, insights, and likely reasons.

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

How it is used:
- Parsed into:
  - `explanation`
  - `insights`
  - `why_analysis`

Failure behavior:
- Falls back to deterministic explanation logic

### 3.6 Suggested Questions

Source:
- [backend/services/llm_service.py:846](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/services/llm_service.py:846)

Task:
- Generate exactly 4 useful starter questions right after CSV upload.

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

How it is used:
- Parsed as JSON
- Truncated to 4 items
- Returned to frontend after upload

Failure behavior:
- Falls back to 4 hard-coded generic questions

## 4. Where Each Prompt Runs in the Product

### Upload flow

Used in:
- [backend/routers/upload.py:50](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/routers/upload.py:50)

Prompt:
- `suggest_questions()`

### Query flow

Used in:
- [backend/routers/query.py:71](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/routers/query.py:71) for `generate_sql()`
- [backend/routers/query.py:232](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/routers/query.py:232) for `explain_result()`
- [backend/routers/query.py:394](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/routers/query.py:394) for `review_sql()`
- [backend/routers/query.py:589](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/routers/query.py:589) for `repair_sql()`

## 5. Mode-Wise Behavior

## 5.1 Shared Query Structure

All query modes broadly follow:

1. Read dataset and selected mode
2. Optionally preprocess before prompt construction
3. Generate SQL with LLM
4. Optionally review/repair SQL with LLM
5. Execute SQL against a mode-specific `data` table
6. Explain results with LLM

Main source:
- [backend/routers/query.py:46](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/routers/query.py:46)

## 5.2 Raw Mode

Mode branch:
- [backend/routers/query.py:178](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/routers/query.py:178)

Behavior:
- Uses original schema and original sample rows
- Generates SQL from raw dataset view
- Executes SQL against raw CSV loaded into DuckDB

Relevant code:
- Default prompt inputs: [backend/routers/query.py:47](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/routers/query.py:47)
- SQL generation: [backend/routers/query.py:71](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/routers/query.py:71)
- Raw execution: [backend/routers/query.py:179](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/routers/query.py:179)
- Raw table build: [backend/services/query_engine.py:25](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/services/query_engine.py:25)

Prompts used:
- `SemanticLayer._resolve_via_llm()`
- `generate_sql()`
- `review_sql()`
- `repair_sql()` if needed
- `explain_result()` if rows are returned

Why output differs:
- No cleaning is applied
- Nulls, duplicates, outliers, and noisy raw values remain

## 5.3 Smart Mode

Mode branches:
- Pre-prompt preprocessing: [backend/routers/query.py:52](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/routers/query.py:52)
- Execution branch: [backend/routers/query.py:139](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/routers/query.py:139)

Behavior:
- Preprocesses dataset before SQL generation
- Updates prompt inputs to use cleaned schema/sample when possible
- Executes SQL against cleaned `data` table

Preprocessing source:
- [backend/services/preprocessing.py:4](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/services/preprocessing.py:4)

Examples of smart-mode cleaning:
- rename columns
- impute missing values
- remove duplicates
- detect/handle outliers

Prompt input changes:
- rename map applied: [backend/routers/query.py:55](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/routers/query.py:55)
- cleaned sample extracted: [backend/routers/query.py:58](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/routers/query.py:58)

Prompts used:
- same prompt functions as raw mode
- but semantic mapping, SQL generation, review, and repair may all see cleaned schema/sample instead of raw

Why output differs:
- prompt input may refer to normalized column names
- query executes on cleaned data
- aggregates can change because duplicates, nulls, or outliers were handled

## 5.4 Scalable Mode

Mode branch:
- [backend/routers/query.py:153](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/routers/query.py:153)

Behavior:
- Generates SQL first
- Then runs Spark or pandas-fallback cleaning pipeline
- Registers cleaned result as table `data`
- Executes SQL against that cleaned table

Scalable pipeline source:
- [backend/services/spark_engine.py:84](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/services/spark_engine.py:84)

Cleaning done in scalable mode:
- numeric null imputation using mean
- duplicate removal
- approximate IQR-style outlier filtering

Important nuance:
- Unlike smart mode, scalable mode does **not** preprocess `llm_schema` and `llm_sample` first
- So the SQL-generation prompt is usually still built from raw schema/sample

Relevant code:
- default prompt inputs stay raw: [backend/routers/query.py:47](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/routers/query.py:47)
- SQL generated before Spark pipeline: [backend/routers/query.py:71](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/routers/query.py:71)
- Spark pipeline starts later: [backend/routers/query.py:155](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/routers/query.py:155)

Prompts used:
- same prompt functions as raw mode
- semantic mapping, SQL generation, review, and repair usually see raw schema/sample
- explanation sees result rows from cleaned scalable execution

Why output differs:
- SQL runs on cleaned Spark or pandas-fallback data
- means may replace missing numeric values
- duplicates may be removed
- outlier rows may be filtered out

## 6. Prompt Differences by Mode

### 6.1 Prompt templates

There are no separate prompt templates for:
- `raw`
- `smart`
- `scalable`

The same functions are reused in all modes:
- `suggest_questions()`
- `SemanticLayer._resolve_via_llm()`
- `generate_sql()`
- `review_sql()`
- `repair_sql()`
- `explain_result()`

### 6.2 Prompt input differences

| Prompt Flow | Raw | Smart | Scalable |
|---|---|---|---|
| Suggested questions | raw schema/sample | raw schema/sample | raw schema/sample |
| Semantic mapping | raw schema/sample | cleaned schema/sample | raw schema/sample |
| NL to SQL | raw schema/sample | cleaned schema/sample | raw schema/sample |
| SQL review | raw schema/sample | cleaned schema/sample | raw schema/sample |
| SQL repair | raw schema/sample | cleaned schema/sample | raw schema/sample |
| Explanation | raw execution result | smart-cleaned execution result | scalable-cleaned execution result |

## 7. Final Takeaway

If the question is:

"Why does output vary if the prompt is mostly the same?"

The answer is:

1. `smart` mode may feed different schema/sample into the prompts before SQL generation.
2. `raw`, `smart`, and `scalable` execute SQL against different underlying `data` tables.
3. The explanation prompt sees different result rows in each mode, so the narrative changes too.

So the current architecture is:
- shared prompt templates
- one configured model
- mode-specific data preprocessing
- mode-specific execution tables

Not:
- three distinct model configurations
- three independent prompt systems
