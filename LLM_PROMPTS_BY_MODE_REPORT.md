# LLM Prompts by Mode Report

This report groups the project's live LLM prompts by execution mode:
- `raw`
- `smart`
- `scalable`

It also includes the upload-time prompt, which is mode-independent.

## Core Finding

There are not 3 different LLM model stacks for the 3 modes.

What actually changes by mode is:
- the data preprocessing path
- the schema/sample sent into some prompts
- the table contents on which the generated SQL is executed

The main SQL prompt template is shared across modes:
- [backend/services/llm_service.py:313](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/services/llm_service.py:313)

The query-mode branching happens here:
- [backend/routers/query.py:43](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/routers/query.py:43)

## Quick Matrix

| Flow | Raw | Smart | Scalable |
|---|---|---|---|
| Suggested questions on upload | Yes | Yes | Yes |
| Semantic mapping | Yes | Yes | Yes |
| NL to SQL | Yes | Yes | Yes |
| SQL review / guardian | Yes | Yes | Yes |
| SQL repair | If needed | If needed | If needed |
| Result explanation | If rows returned | If rows returned | If rows returned |
| Prompt input schema/sample changes by mode | No | Yes | Mostly no |
| SQL executes on cleaned table | No | Yes | Yes |

## Shared Query Flow

All query modes follow the same high-level order:

1. Load dataset and mode
2. Optionally preprocess before prompt construction
3. Build SQL with the LLM
4. Optionally review and repair SQL with the LLM
5. Execute SQL against a mode-specific `data` table
6. Explain the result with the LLM

Main source:
- [backend/routers/query.py:46](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/routers/query.py:46)

## Upload-Time Prompt

This prompt is not tied to `raw`, `smart`, or `scalable`. It runs during CSV upload.

Used in:
- [backend/routers/upload.py:50](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/routers/upload.py:50)

Prompt flow:
- `suggest_questions()`
- Source: [backend/services/llm_service.py:846](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/services/llm_service.py:846)

Task:
- Generate exactly 4 suggested questions from schema + sample rows.

Prompt characteristics:
- Same for every upload
- Uses raw analyzed schema and sample from the uploaded CSV

Expected output:

```json
[
  "Question 1",
  "Question 2",
  "Question 3",
  "Question 4"
]
```

## Raw Mode

Mode branch:
- [backend/routers/query.py:178](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/routers/query.py:178)

### What Raw Mode Does

- Uses the original dataset schema and original sample rows as prompt input
- Generates SQL from the unprocessed dataset view
- Executes SQL on the raw CSV loaded into DuckDB
- Explains the result after execution

Relevant code:
- Initial prompt inputs: [backend/routers/query.py:47](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/routers/query.py:47)
- SQL generation call: [backend/routers/query.py:71](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/routers/query.py:71)
- Raw execution: [backend/routers/query.py:179](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/routers/query.py:179)
- DuckDB raw table creation: [backend/services/query_engine.py:25](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/services/query_engine.py:25)

### LLM Prompts Used in Raw Mode

1. Semantic mapping
- Source: [backend/services/llm_service.py:214](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/services/llm_service.py:214)
- Input basis: raw schema + raw sample
- Output: JSON term-to-column mappings

2. NL to SQL
- Source: [backend/services/llm_service.py:274](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/services/llm_service.py:274)
- Input basis: raw schema + raw sample + question + history
- Output: DuckDB SQL string

3. SQL review
- Source: [backend/services/llm_service.py:366](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/services/llm_service.py:366)
- Input basis: raw schema + raw sample + candidate SQL
- Output: JSON verdict / reason / optional fixed SQL

4. SQL repair
- Source: [backend/services/llm_service.py:462](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/services/llm_service.py:462)
- Input basis: raw schema + raw sample + failed SQL + error reason
- Output: corrected SQL string

5. Result explanation
- Source: [backend/services/llm_service.py:774](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/services/llm_service.py:774)
- Input basis: executed SQL + result preview
- Output: explanation, insights, why-analysis sections

### Why Raw Mode Output Can Differ

- No cleaning is applied before execution
- Missing values, duplicate rows, noisy column names, and outliers remain in the table
- So even if SQL shape matches other modes, returned aggregates and row counts may differ

## Smart Mode

Mode branch:
- Pre-prompt preprocessing: [backend/routers/query.py:52](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/routers/query.py:52)
- Execution branch: [backend/routers/query.py:139](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/routers/query.py:139)

### What Smart Mode Does

- Preprocesses the dataset before LLM SQL generation
- May normalize column names
- May impute missing values
- May remove duplicates
- May detect and act on outliers
- Uses the cleaned table and adjusted prompt inputs together

Preprocessing pipeline source:
- [backend/services/preprocessing.py:4](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/services/preprocessing.py:4)

Examples of preprocessing capabilities:
- Rename map: [backend/services/preprocessing.py:160](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/services/preprocessing.py:160)
- Duplicate removal: [backend/services/preprocessing.py:701](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/services/preprocessing.py:701)
- Imputation: [backend/services/preprocessing.py:804](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/services/preprocessing.py:804)
- Outlier handling: [backend/services/preprocessing.py:906](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/services/preprocessing.py:906)

### How Prompt Inputs Change in Smart Mode

Smart mode is the only mode that explicitly adjusts LLM prompt inputs before SQL generation.

Code:
- Preprocess call: [backend/routers/query.py:54](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/routers/query.py:54)
- Schema rename application: [backend/routers/query.py:55](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/routers/query.py:55)
- Cleaned sample extraction: [backend/routers/query.py:58](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/routers/query.py:58)

That means these prompt inputs may differ from raw mode:
- column names
- sample row values
- null patterns
- outlier presence

### LLM Prompts Used in Smart Mode

The same prompt functions run as in raw mode, but some prompt inputs are changed first.

1. Semantic mapping
- Same prompt template as raw mode
- Input basis: cleaned or renamed schema + cleaned sample

2. NL to SQL
- Same prompt template as raw mode
- Input basis: cleaned or renamed schema + cleaned sample + question + history

3. SQL review
- Same prompt template as raw mode
- Input basis: cleaned or renamed schema + cleaned sample + candidate SQL

4. SQL repair
- Same prompt template as raw mode
- Input basis: cleaned or renamed schema + cleaned sample + failed SQL + error reason

5. Result explanation
- Same prompt template as raw mode
- Input basis: SQL executed against cleaned `data` table + result preview

### Why Smart Mode Output Can Differ

- The prompt may mention renamed columns instead of raw column names
- The sample shown to the model may already contain imputed values
- SQL executes on a cleaned table, not the original CSV
- Aggregates can change because duplicates or outliers may have been removed or transformed

## Scalable Mode

Mode branch:
- [backend/routers/query.py:153](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/routers/query.py:153)

### What Scalable Mode Does

- Generates SQL first using the default prompt inputs
- Then runs a Spark-style cleaning pipeline
- Registers the cleaned Spark/pandas result as table `data`
- Executes the SQL on that cleaned table

Scalable preprocessing source:
- [backend/services/spark_engine.py:84](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/services/spark_engine.py:84)

Spark cleaning steps:
- numeric null imputation using mean
- duplicate removal
- approximate IQR-style outlier filtering

Code:
- Mean fill: [backend/services/spark_engine.py:121](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/services/spark_engine.py:121)
- Drop duplicates: [backend/services/spark_engine.py:134](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/services/spark_engine.py:134)
- Outlier filter: [backend/services/spark_engine.py:136](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/services/spark_engine.py:136)
- Fallback path: [backend/services/spark_engine.py:162](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/services/spark_engine.py:162)

### How Prompt Inputs Change in Scalable Mode

Important nuance:
- `scalable` mode does not preprocess `llm_schema` and `llm_sample` before SQL generation the way `smart` mode does
- So the main SQL prompt is usually built from the raw schema/sample

Relevant code:
- Default prompt inputs remain raw: [backend/routers/query.py:47](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/routers/query.py:47)
- SQL generated before Spark pipeline runs: [backend/routers/query.py:71](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/routers/query.py:71)
- Spark pipeline starts later: [backend/routers/query.py:155](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/routers/query.py:155)

So scalable mode differs from smart mode like this:
- `smart`: prompt input and execution table both change
- `scalable`: execution table changes, but prompt input mostly stays raw

### LLM Prompts Used in Scalable Mode

1. Semantic mapping
- Same prompt template as raw mode
- Input basis: raw schema + raw sample

2. NL to SQL
- Same prompt template as raw mode
- Input basis: raw schema + raw sample + question + history

3. SQL review
- Same prompt template as raw mode
- Input basis: raw schema + raw sample + candidate SQL

4. SQL repair
- Same prompt template as raw mode
- Input basis: raw schema + raw sample + failed SQL + error reason

5. Result explanation
- Same prompt template as raw mode
- Input basis: SQL executed against Spark-cleaned or pandas-fallback-cleaned `data` table + result preview

### Why Scalable Mode Output Can Differ

- SQL runs on a cleaned table produced by Spark or pandas fallback
- Numeric nulls may be filled with mean values
- Duplicate rows may be dropped
- Outlier rows may be filtered out
- Even though the prompt itself is mostly raw-input based, the executed result can still differ materially

## Prompt Differences by Mode

### Prompt template differences

There are no separate prompt templates for:
- `raw`
- `smart`
- `scalable`

The same LLM functions are reused:
- `suggest_questions()`
- `SemanticLayer._resolve_via_llm()`
- `generate_sql()`
- `review_sql()`
- `repair_sql()`
- `explain_result()`

All of these live in:
- [backend/services/llm_service.py](/d:/Hackathon/natwest hackathon/latest/code_for_purpose/backend/services/llm_service.py:1)

### Prompt input differences

| Prompt Flow | Raw | Smart | Scalable |
|---|---|---|---|
| Suggested questions | raw schema/sample | raw schema/sample | raw schema/sample |
| Semantic mapping | raw schema/sample | cleaned schema/sample | raw schema/sample |
| NL to SQL | raw schema/sample | cleaned schema/sample | raw schema/sample |
| SQL review | raw schema/sample | cleaned schema/sample | raw schema/sample |
| SQL repair | raw schema/sample | cleaned schema/sample | raw schema/sample |
| Explanation | result rows from raw execution | result rows from cleaned smart execution | result rows from scalable cleaned execution |

## Final Takeaway

If you are asking, "Why does the output vary if the prompt is the same?", the answer is:

1. The SQL may be generated from different schema/sample inputs in `smart` mode.
2. The SQL is executed against different underlying `data` tables across modes.
3. The result-explanation prompt sees different result rows, so the explanation changes too.

So the project currently has:
- shared prompt templates
- mode-specific data processing
- mode-dependent execution tables

Not:
- three distinct LLM prompt systems
- three distinct model configurations
