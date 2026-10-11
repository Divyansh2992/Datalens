"""
LLM Service — Groq-powered NL→SQL generation and result explanation.
Uses openai/gpt-oss-120b by default (override with GROQ_MODEL).
Schema + sample (≤5 rows) are sent — never the full dataset.

NEW:
  • SQL Validator  — blocks DROP, DELETE, INSERT, UPDATE, DDL, etc.
  • Semantic Layer — resolves business terms to actual column names via Groq
                     so "revenue" → amount, "customer" → user_id, etc.
"""
import os
import re
import json
from typing import List, Dict, Any, Tuple, Optional

from groq import Groq
from dotenv import load_dotenv

load_dotenv()

_client = Groq(api_key=os.environ.get("GROQ_API_KEY", ""))
MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
_REASONING_TOKEN_HEADROOM = 1024


def _chat_completion(**kwargs: Any):
    """
    Call Groq chat completions. gpt-oss models spend completion tokens on
    reasoning, so keep effort low and add headroom so the answer isn't cut off.
    """
    if kwargs.get("model", "").startswith("openai/gpt-oss"):
        kwargs.setdefault("reasoning_effort", "low")
        kwargs["max_tokens"] = kwargs.get("max_tokens", 0) + _REASONING_TOKEN_HEADROOM
    return _client.chat.completions.create(**kwargs)


# ══════════════════════════════════════════════════════════════════════════════
# SQL VALIDATOR
# ══════════════════════════════════════════════════════════════════════════════

# Patterns that must NEVER appear in generated SQL
_DANGEROUS_PATTERNS: List[re.Pattern] = [
    re.compile(r"\bDROP\b",        re.IGNORECASE),
    re.compile(r"\bDELETE\b",      re.IGNORECASE),
    re.compile(r"\bTRUNCATE\b",    re.IGNORECASE),
    re.compile(r"\bINSERT\b",      re.IGNORECASE),
    re.compile(r"\bUPDATE\b",      re.IGNORECASE),
    re.compile(r"\bALTER\b",       re.IGNORECASE),
    re.compile(r"\bCREATE\b",      re.IGNORECASE),
    # Allow scalar REPLACE(...) in SELECT expressions, but block MySQL-style write op.
    re.compile(r"\bREPLACE\s+INTO\b", re.IGNORECASE),
    re.compile(r"\bMERGE\b",       re.IGNORECASE),
    re.compile(r"\bEXECUTE\b",     re.IGNORECASE),
    re.compile(r"\bEXEC\b",        re.IGNORECASE),
    re.compile(r"\bCALL\b",        re.IGNORECASE),
    re.compile(r"\bGRANT\b",       re.IGNORECASE),
    re.compile(r"\bREVOKE\b",      re.IGNORECASE),
    re.compile(r"\bCOPY\b",        re.IGNORECASE),   # DuckDB COPY TO / FROM
    re.compile(r"\bATTACH\b",      re.IGNORECASE),   # DuckDB ATTACH DATABASE
    re.compile(r"\bDETACH\b",      re.IGNORECASE),
    re.compile(r"\bLOAD\b",        re.IGNORECASE),   # DuckDB LOAD extension
    re.compile(r"\bINSTALL\b",     re.IGNORECASE),   # DuckDB INSTALL extension
    re.compile(r"--",                              ),  # SQL comments (injection vector)
    re.compile(r"/\*",                             ),  # Block comment open
    re.compile(r"\bINTO\s+OUTFILE\b", re.IGNORECASE), # MySQL-style file write
    re.compile(r"\bxp_cmdshell\b", re.IGNORECASE),    # MSSQL shell
    re.compile(r"\bINFORMATION_SCHEMA\b", re.IGNORECASE),  # meta-tables
    re.compile(r"\bpg_\w+",        re.IGNORECASE),    # postgres internals
    re.compile(r"\bsqlite_\w+",    re.IGNORECASE),    # sqlite internals
]

# The ONLY DML keyword we allow
_ALLOWED_LEADING_KEYWORDS = re.compile(
    r"^\s*(SELECT|WITH)\b", re.IGNORECASE
)


class SQLValidationError(ValueError):
    """Raised when generated SQL fails safety checks."""
    pass


def validate_sql(sql: str) -> str:
    """
    Validate that a SQL string is read-only and safe to execute.

    Rules:
      1. Must start with SELECT or a CTE (WITH … SELECT).
      2. Must not contain any destructive / DDL / admin keyword.
      3. Must not contain comment sequences (injection vectors).

    Returns the original sql unchanged if valid.
    Raises SQLValidationError with a human-readable reason if not.
    """
    stripped = sql.strip()

    # Rule 1 — must open with SELECT or WITH
    if not _ALLOWED_LEADING_KEYWORDS.match(stripped):
        first_word = stripped.split()[0].upper() if stripped.split() else "?"
        raise SQLValidationError(
            f"Only SELECT queries are allowed. Got '{first_word}' instead."
        )

    # Rule 2 & 3 — no dangerous keywords or comment markers
    for pattern in _DANGEROUS_PATTERNS:
        if pattern.search(stripped):
            matched = pattern.pattern
            raise SQLValidationError(
                f"SQL contains a forbidden keyword or pattern: '{matched}'. "
                "Only read-only SELECT queries are permitted."
            )

    return sql


# ══════════════════════════════════════════════════════════════════════════════
# SEMANTIC LAYER
# ══════════════════════════════════════════════════════════════════════════════

class SemanticLayer:
    """
    Maintains a business-term → column mapping and uses the Groq LLM to
    infer new mappings on the fly from schema + sample data.

    Usage:
        sem = SemanticLayer(schema, sample)
        enriched_question = sem.enrich(user_question)

    The enriched question appends a mapping hint so the SQL-generation LLM
    knows which actual column names to use.
    """

    def __init__(
        self,
        schema: List[Dict[str, Any]],
        sample: List[Dict[str, Any]],
        extra_mappings: Optional[Dict[str, str]] = None,
    ) -> None:
        self.schema = schema
        self.sample = sample
        # Start with any hard-coded project-specific aliases
        self._mappings: Dict[str, str] = dict(extra_mappings or {})
        self._column_names: List[str] = [c["name"] for c in schema]

    # ── Public ────────────────────────────────────────────────────────────────

    def enrich(self, question: str) -> str:
        """
        Detect business terms in the question that don't literally match any
        column name, resolve them via the LLM, and return the question with
        an appended mapping hint.
        """
        unmapped_terms = self._detect_unmapped_terms(question)

        if unmapped_terms:
            new_mappings = self._resolve_via_llm(unmapped_terms)
            self._mappings.update(new_mappings)

        if not self._mappings:
            return question  # nothing to annotate

        active = {
            term: col
            for term, col in self._mappings.items()
            if re.search(rf"\b{re.escape(term)}\b", question, re.IGNORECASE)
        }

        if not active:
            return question

        hint_parts = [f'"{term}" refers to column "{col}"' for term, col in active.items()]
        hint = "Note — semantic mappings: " + "; ".join(hint_parts) + "."
        return f"{question}\n\n[{hint}]"

    def get_mappings(self) -> Dict[str, str]:
        """Return a copy of all currently known business-term mappings."""
        return dict(self._mappings)

    def add_mapping(self, term: str, column: str) -> None:
        """Manually register or override a business-term → column mapping."""
        self._mappings[term.lower()] = column

    # ── Private ───────────────────────────────────────────────────────────────

    def _detect_unmapped_terms(self, question: str) -> List[str]:
        """
        Extract noun-like tokens from the question that:
          (a) are not a literal column name (case-insensitive), and
          (b) are not already in our mappings dict.
        We use a light heuristic — words ≥4 chars that look like domain nouns.
        """
        col_names_lower = {c.lower() for c in self._column_names}
        known_lower = set(self._mappings.keys())

        # Tokenise: lower-case alpha words ≥ 4 chars
        tokens = re.findall(r"\b[a-zA-Z]{4,}\b", question)
        # Stop-words we never want to resolve
        stopwords = {
            "show", "give", "list", "what", "which", "where", "when", "have",
            "with", "this", "that", "from", "each", "also", "only", "more",
            "than", "over", "into", "been", "were", "does", "data", "table",
            "query", "rows", "column", "columns", "total", "count", "average",
            "group", "order", "limit", "percent", "number", "values", "plot",
            "chart", "graph", "between", "compare", "versus", "against",
        }

        candidates = []
        seen = set()
        for tok in tokens:
            tl = tok.lower()
            if tl in seen or tl in stopwords:
                continue
            seen.add(tl)
            if tl not in col_names_lower and tl not in known_lower:
                candidates.append(tok)

        return candidates

    def _resolve_via_llm(self, terms: List[str]) -> Dict[str, str]:
        """
        Ask the Groq LLM which column (if any) each business term maps to.
        Returns only terms that confidently map to an existing column.
        """
        schema_lines = "\n".join(
            f"  - {c['name']} ({c['type']})" for c in self.schema
        )
        sample_str = json.dumps(_serialise(self.sample[:3]), indent=2)

        prompt = (
            f"You are a data dictionary expert.\n\n"
            f"Table columns:\n{schema_lines}\n\n"
            f"Sample rows:\n{sample_str}\n\n"
            f"Business terms to map: {json.dumps(terms)}\n\n"
            "For each business term, find the SINGLE best matching column name "
            "from the table above. If there is no reasonable match, omit that term.\n\n"
            "Return ONLY a valid JSON object mapping each resolved term to its column name. "
            "No markdown, no explanation. Example:\n"
            '{"revenue": "amount", "customer": "user_id"}'
        )

        try:
            response = _chat_completion(
                model=MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a precise data dictionary assistant. "
                            "Return only a JSON object, nothing else."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                max_tokens=300,
            )

            raw = response.choices[0].message.content.strip()
            raw = re.sub(r"```json\s*|```", "", raw).strip()
            mappings: Dict[str, str] = json.loads(raw)

            # Safety: only keep mappings whose target column actually exists
            valid = {
                term.lower(): col
                for term, col in mappings.items()
                if col in self._column_names
            }
            return valid

        except Exception:
            # Semantic resolution is best-effort — never crash the pipeline
            return {}


# ══════════════════════════════════════════════════════════════════════════════
# SQL Generation  (now uses SemanticLayer + validate_sql)
# ══════════════════════════════════════════════════════════════════════════════

def generate_sql(
    schema: List[Dict[str, Any]],
    sample: List[Dict[str, Any]],
    question: str,
    history: List[Dict[str, str]] = [],
    semantic_layer: Optional[SemanticLayer] = None,
) -> str:
    """
    Convert a natural language question to a DuckDB SQL query.

    Steps:
      1. Enrich the question with semantic mappings (if a SemanticLayer is given).
      2. Call the Groq LLM to produce SQL.
      3. Validate the SQL is read-only and safe.

    The table is always named "data".
    Returns a clean, validated SQL string (no markdown, no explanation).
    Raises SQLValidationError if the LLM produces unsafe SQL.
    """
    # ── 1. Semantic enrichment ────────────────────────────────────────────────
    if semantic_layer is None:
        semantic_layer = SemanticLayer(schema, sample)

    enriched_question = semantic_layer.enrich(question)

    # ── 2. Build prompt ───────────────────────────────────────────────────────
    schema_lines = "\n".join(
        f"  - {c['name']} ({c['type']}, null%={c.get('null_pct', 0):.1f}"
        + (f", mean={c['mean']:.2f}" if c.get("mean") is not None else "")
        + ")"
        for c in schema
    )
    sample_str = json.dumps(_serialise(sample[:5]), indent=2)

    system_prompt = (
        "You are an expert DuckDB SQL analyst.\n"
        "RULES:\n"
        "1. The table name is ALWAYS 'data' — never use any other table name.\n"
        "2. Return ONLY the SQL query — no markdown, no explanation, no preamble.\n"
        "3. Always quote column names with double-quotes to handle spaces/special chars.\n"
        "4. Use DuckDB-compatible syntax (e.g. PERCENTILE_CONT for percentiles).\n"
        "5. For comparative questions (e.g. 'versus', 'compare'), ensure you GROUP BY "
        "the relevant column to show a comparison.\n"
        "6. If the user asks for a plot, graph, or chart, ensure the query returns at "
        "least one categorical/date column and one numeric column for visualization.\n"
        "7. Keep the query concise and efficient.\n"
        "8. NEVER use DROP, DELETE, INSERT, UPDATE, ALTER, CREATE or any DDL/DML that "
        "modifies data. Only SELECT statements are allowed.\n\n"
        f"Table schema:\n{schema_lines}\n\n"
        f"Sample rows (first 5):\n{sample_str}"
    )

    messages = [{"role": "system", "content": system_prompt}]

    for h in history[-6:]:
        messages.append(h)

    messages.append({"role": "user", "content": f"Question: {enriched_question}"})

    # ── 3. Call LLM ───────────────────────────────────────────────────────────
    response = _chat_completion(
        model=MODEL,
        messages=messages,
        temperature=0.05,
        max_tokens=800,
    )

    raw = response.choices[0].message.content.strip()
    sql = _clean_sql(raw)

    # ── 4. Validate (raises SQLValidationError if unsafe) ─────────────────────
    validate_sql(sql)

    return sql


# ══════════════════════════════════════════════════════════════════════════════
# SQL Guardian helpers
# ══════════════════════════════════════════════════════════════════════════════

def review_sql(
    schema: List[Dict[str, Any]],
    sample: List[Dict[str, Any]],
    question: str,
    sql: str,
) -> Dict[str, Any]:
    """
    Ask the LLM to verify whether SQL is semantically aligned with the user
    question. Returns a JSON-like dict:

      {
        "verdict": "PASS" | "FAIL",
        "reason": "...",
        "fixed_sql": "...optional..."
      }

    This method is best-effort; on verifier failure it returns PASS so the
    non-LLM guards can continue.
    """
    schema_lines = "\n".join(
        f"  - {c['name']} ({c['type']})" for c in schema
    )
    sample_str = json.dumps(_serialise(sample[:3]), indent=2)

    prompt = (
        "You are a strict SQL verifier for DuckDB queries.\n\n"
        f"User question:\n{question}\n\n"
        f"Candidate SQL:\n{sql}\n\n"
        f"Table schema:\n{schema_lines}\n\n"
        f"Sample rows:\n{sample_str}\n\n"
        "Task:\n"
        "1. Decide if SQL answers the user's intent.\n"
        "2. Check if selected/grouped columns and filters are logically correct.\n"
        "3. If wrong, provide one corrected SQL query for DuckDB.\n\n"
        "Return ONLY a JSON object in this format:\n"
        '{"verdict":"PASS|FAIL","reason":"short reason","fixed_sql":"corrected query or empty"}'
    )

    try:
        response = _chat_completion(
            model=MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You verify SQL correctness with strict reasoning. "
                        "Return only valid JSON."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_tokens=450,
        )

        raw = response.choices[0].message.content.strip()
        parsed = _parse_json_object(raw)

        verdict = str(parsed.get("verdict", "PASS")).upper()
        reason = str(parsed.get("reason", "No issues detected.")).strip()
        fixed_sql = str(parsed.get("fixed_sql", "")).strip()

        if verdict not in {"PASS", "FAIL"}:
            verdict = "PASS"

        if fixed_sql:
            fixed_sql = _clean_sql(fixed_sql)
            try:
                validate_sql(fixed_sql)
            except SQLValidationError:
                fixed_sql = ""

        return {
            "verdict": verdict,
            "reason": reason or "No issues detected.",
            "fixed_sql": fixed_sql,
        }

    except Exception:
        return {
            "verdict": "PASS",
            "reason": "Verifier unavailable; continuing with generated SQL.",
            "fixed_sql": "",
        }


def repair_sql(
    schema: List[Dict[str, Any]],
    sample: List[Dict[str, Any]],
    question: str,
    failed_sql: str,
    error_reason: str,
    history: List[Dict[str, str]] = [],
) -> str:
    """
    Generate a corrected SQL query after a validator/runtime/verifier failure.
    Returns a cleaned and validated SQL string.
    """
    schema_lines = "\n".join(
        f"  - {c['name']} ({c['type']})" for c in schema
    )
    sample_str = json.dumps(_serialise(sample[:5]), indent=2)

    system_prompt = (
        "You are an expert DuckDB SQL repair assistant.\n"
        "RULES:\n"
        "1. Table name is always data.\n"
        "2. Return only SQL.\n"
        "3. Use only read-only SELECT/CTE queries.\n"
        "4. Quote column names using double-quotes.\n"
        "5. Keep the query concise and correct.\n\n"
        f"Table schema:\n{schema_lines}\n\n"
        f"Sample rows:\n{sample_str}"
    )

    user_prompt = (
        f"Original question: {question}\n\n"
        f"Failed SQL:\n{failed_sql}\n\n"
        f"Failure reason:\n{error_reason}\n\n"
        "Return a corrected DuckDB SQL query that answers the original question."
    )

    messages = [{"role": "system", "content": system_prompt}]
    for h in history[-4:]:
        messages.append(h)
    messages.append({"role": "user", "content": user_prompt})

    response = _chat_completion(
        model=MODEL,
        messages=messages,
        temperature=0.05,
        max_tokens=800,
    )

    sql = _clean_sql(response.choices[0].message.content.strip())
    validate_sql(sql)
    return sql


# ══════════════════════════════════════════════════════════════════════════════
# Explanation & Insights  (unchanged logic, kept for completeness)
# ══════════════════════════════════════════════════════════════════════════════

def explain_result(
    question: str,
    sql: str,
    result: List[Dict[str, Any]],
    columns: List[str],
) -> Dict[str, Any]:
    """
    Generate a plain-English explanation, bullet insights, and 'Why did this happen?'
    from the query result.
    """
    if not result:
        return {
            "explanation": "No rows matched the query conditions.",
            "insights": ["Try broadening filters or removing restrictive conditions."],
            "why_analysis": "The combination of selected filters likely narrowed the dataset to zero matching rows.",
        }

    preview_rows = _serialise(result[:20])
    result_preview = json.dumps(preview_rows, indent=2)
    cols_line = ", ".join(columns or [])

    prompt = (
        f'The user asked: "{question}"\n'
        f"SQL executed: {sql}\n"
        f"Result columns: {cols_line}\n"
        f"Result preview (up to 20 rows):\n{result_preview}\n\n"
        "Provide the following — use EXACTLY the section headers shown:\n\n"
        "EXPLANATION: <2-4 sentences in plain English summarising the key finding and scale>\n\n"
        "INSIGHTS:\n"
        "• <insight 1 with concrete value/metric>\n"
        "• <insight 2 with concrete value/metric>\n"
        "• <insight 3 (trend, spread, or anomaly)>\n"
        "• <insight 4 if applicable>\n\n"
        "WHY: <1-2 sentences on likely drivers rooted in the observed numbers>"
    )

    try:
        response = _chat_completion(
            model=MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a concise, insightful data analyst. "
                        "Always follow the exact section format. "
                        "Ground every claim in the provided rows and never invent metrics."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            max_tokens=850,
        )

        content = response.choices[0].message.content
        parsed = _parse_explanation(content)
        if _is_weak_explanation(parsed.get("explanation", "")):
            fallback = _deterministic_explanation(question, result, columns)
            if not parsed.get("insights"):
                parsed["insights"] = fallback["insights"]
            parsed["explanation"] = fallback["explanation"]
            if not parsed.get("why_analysis"):
                parsed["why_analysis"] = fallback["why_analysis"]
        return parsed
    except Exception:
        return _deterministic_explanation(question, result, columns)


# ══════════════════════════════════════════════════════════════════════════════
# Auto-Suggested Questions  (unchanged)
# ══════════════════════════════════════════════════════════════════════════════

def suggest_questions(
    schema: List[Dict[str, Any]],
    sample: List[Dict[str, Any]],
) -> List[str]:
    """Generate 4 interesting questions the user might want to ask about their dataset."""
    schema_lines = "\n".join(f"  - {c['name']} ({c['type']})" for c in schema)
    sample_str = json.dumps(_serialise(sample[:3]), indent=2)

    response = _chat_completion(
        model=MODEL,
        messages=[
            {
                "role": "system",
                "content": "You are a data analyst suggesting insightful questions about a dataset.",
            },
            {
                "role": "user",
                "content": (
                    f"Schema:\n{schema_lines}\n\n"
                    f"Sample:\n{sample_str}\n\n"
                    "Suggest exactly 4 interesting, specific questions a business analyst "
                    "would want to ask about this dataset.\n"
                    "Return ONLY a JSON array of 4 strings. No other text."
                ),
            },
        ],
        temperature=0.5,
        max_tokens=350,
    )

    try:
        raw = response.choices[0].message.content.strip()
        raw = re.sub(r"```json\s*", "", raw)
        raw = re.sub(r"```\s*", "", raw)
        questions = json.loads(raw)
        return [str(q) for q in questions[:4]]
    except Exception:
        return [
            "What are the top 5 records by value?",
            "Show average values grouped by category.",
            "Are there any trends over time?",
            "Which group has the highest total?",
        ]


# ══════════════════════════════════════════════════════════════════════════════
# Private helpers
# ══════════════════════════════════════════════════════════════════════════════

def _clean_sql(raw: str) -> str:
    """Strip markdown fences and leading/trailing whitespace."""
    raw = re.sub(r"```sql\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"```\s*", "", raw)
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    return "\n".join(lines).strip()


def _parse_json_object(raw: str) -> Dict[str, Any]:
    """Parse first JSON object from raw model output."""
    text = re.sub(r"```json\s*|```", "", raw, flags=re.IGNORECASE).strip()
    try:
        return json.loads(text)
    except Exception:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def _parse_explanation(content: str) -> Dict[str, Any]:
    """Parse the structured LLM response into explanation, insights, and why."""
    explanation = ""
    insights: List[str] = []
    why = ""
    current = None

    lines = content.splitlines()
    for line in lines:
        line = line.strip()
        upper = line.upper()

        if upper.startswith("EXPLANATION:"):
            explanation = line[len("EXPLANATION:"):].strip()
            current = "explanation"
        elif upper.startswith("**EXPLANATION:**"):
            explanation = line[len("**EXPLANATION:**"):].strip()
            current = "explanation"
        elif upper.startswith("INSIGHTS:"):
            current = "insights"
        elif upper.startswith("**INSIGHTS:**"):
            current = "insights"
        elif upper.startswith("WHY:"):
            why = line[len("WHY:"):].strip()
            current = "why"
        elif upper.startswith("**WHY:**"):
            why = line[len("**WHY:**"):].strip()
            current = "why"
        elif (
            (line.startswith("•") or line.startswith("-") or re.match(r"^\d+\.", line))
            and current == "insights"
        ):
            txt = re.sub(r"^[•\-\d\.]+\s*", "", line).strip()
            if txt:
                insights.append(txt)
        elif current == "explanation" and line and not any(
            h in upper for h in ["INSIGHTS:", "WHY:"]
        ):
            explanation = (explanation + " " + line).strip()
        elif current == "why" and line:
            why = (why + " " + line).strip()

    if not explanation and lines:
        explanation = lines[0].strip()
        if len(lines) > 1 and not lines[1].strip().startswith(("-", "•")):
            explanation += " " + lines[1].strip()

    return {
        "explanation": explanation or "Query executed successfully.",
        "insights": insights[:5],
        "why_analysis": why,
    }


def _is_weak_explanation(text: str) -> bool:
    cleaned = (text or "").strip().lower()
    if not cleaned:
        return True
    weak_phrases = {
        "query executed successfully.",
        "query executed successfully",
        "here are the results",
        "the query returned results",
    }
    if cleaned in weak_phrases:
        return True
    return len(cleaned) < 35


def _deterministic_explanation(
    question: str,
    result: List[Dict[str, Any]],
    columns: List[str],
) -> Dict[str, Any]:
    rows = result[:200]
    row_count = len(rows)
    if row_count == 0:
        return {
            "explanation": "No rows matched the query conditions.",
            "insights": ["Try broadening filters or removing restrictive conditions."],
            "why_analysis": "The selected filters likely narrowed the dataset to zero matching rows.",
        }

    numeric_cols = []
    for col in columns:
        nums = [_to_float(r.get(col)) for r in rows]
        valid = [n for n in nums if n is not None]
        if valid and len(valid) / max(1, row_count) >= 0.6:
            numeric_cols.append(col)

    primary_num = numeric_cols[0] if numeric_cols else None
    primary_dim = next((c for c in columns if c != primary_num), columns[0] if columns else None)

    insights: List[str] = []
    why = ""

    if primary_num:
        values = [_to_float(r.get(primary_num)) for r in rows]
        values = [v for v in values if v is not None]
        avg_v = sum(values) / len(values) if values else 0.0
        min_v = min(values) if values else 0.0
        max_v = max(values) if values else 0.0

        explanation = (
            f"The query returned {row_count} row{'s' if row_count != 1 else ''}. "
            f"The primary metric \"{primary_num}\" averages {_fmt_num(avg_v)} "
            f"with a range from {_fmt_num(min_v)} to {_fmt_num(max_v)}."
        )

        insights.append(
            f"\"{primary_num}\" spread: min {_fmt_num(min_v)}, max {_fmt_num(max_v)}, avg {_fmt_num(avg_v)}."
        )

        if primary_dim:
            top_row = max(rows, key=lambda r: _to_float(r.get(primary_num)) or float("-inf"))
            low_row = min(rows, key=lambda r: _to_float(r.get(primary_num)) or float("inf"))
            top_dim = top_row.get(primary_dim)
            low_dim = low_row.get(primary_dim)
            insights.append(
                f"Highest \"{primary_num}\" appears at \"{primary_dim}\"={top_dim} ({_fmt_num(_to_float(top_row.get(primary_num)) or 0)})."
            )
            insights.append(
                f"Lowest \"{primary_num}\" appears at \"{primary_dim}\"={low_dim} ({_fmt_num(_to_float(low_row.get(primary_num)) or 0)})."
            )

        volatility = (max_v - min_v) / abs(avg_v) if avg_v else 0
        if volatility > 1.5:
            why = "The wide spread suggests uneven performance across groups or periods in the returned subset."
        elif volatility > 0.6:
            why = "Variation is moderate, indicating some meaningful differences across the returned rows."
        else:
            why = "Values are relatively stable in this slice, so no strong dispersion pattern is visible."
    else:
        key_col = columns[0] if columns else "value"
        uniques = len({str(r.get(key_col)) for r in rows}) if key_col else row_count
        explanation = (
            f"The query returned {row_count} row{'s' if row_count != 1 else ''} "
            f"across approximately {uniques} distinct \"{key_col}\" values."
        )
        insights = [
            f"Result contains categorical/text-heavy output with {row_count} records.",
            "Add an aggregate metric (COUNT, SUM, AVG) for deeper quantitative insights.",
        ]
        why = "The output appears descriptive rather than numeric, so the key signal is distribution across returned labels."

    if not insights:
        insights = ["The query completed successfully and returned interpretable rows."]

    return {
        "explanation": explanation,
        "insights": insights[:5],
        "why_analysis": why,
    }


def _to_float(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except Exception:
        return None


def _fmt_num(value: float) -> str:
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if abs(value) >= 1_000:
        return f"{value / 1_000:.2f}k"
    return f"{value:.2f}"


def _serialise(obj: Any) -> Any:
    """Make Python objects JSON-serialisable (handle NaN, timestamps, etc.)."""
    import math
    import datetime

    if isinstance(obj, list):
        return [_serialise(item) for item in obj]
    if isinstance(obj, dict):
        return {k: _serialise(v) for k, v in obj.items()}
    if isinstance(obj, float) and math.isnan(obj):
        return None
    if isinstance(obj, (datetime.date, datetime.datetime)):
        return str(obj)
    return obj
