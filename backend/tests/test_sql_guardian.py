import pytest

from routers import query as query_router
from services import llm_service


def _payload(dataset_id, question="test guardian behavior"):
    return {
        "dataset_id": dataset_id,
        "question": question,
        "mode": "raw",
        "guardian_enabled": True,
    }


def _raise_repair_failure(*_args, **_kwargs):
    raise RuntimeError("forced repair failure")


def test_guardian_blocks_unsafe_sql_without_repair(client, seeded_dataset, monkeypatch):
    monkeypatch.setattr(
        query_router.llm_service,
        "generate_sql",
        lambda *_args, **_kwargs: "SELECT 1 -- unsafe comment",
    )
    monkeypatch.setattr(query_router.llm_service, "repair_sql", _raise_repair_failure)

    response = client.post("/query", json=_payload(seeded_dataset))
    body = response.json()

    assert response.status_code == 200
    assert body["guardian_enabled"] is True
    assert body["guardian_passed"] is False
    assert body["error"] == "SQL Guardian could not repair an unsafe query."
    assert any("static SQL safety check failed" in line for line in body["guardian_log"])


def test_guardian_blocks_semantic_fail_when_repair_fails(client, seeded_dataset, monkeypatch):
    monkeypatch.setattr(
        query_router.llm_service,
        "generate_sql",
        lambda *_args, **_kwargs: 'SELECT "x" FROM data',
    )
    monkeypatch.setattr(
        query_router.llm_service,
        "review_sql",
        lambda *_args, **_kwargs: {
            "verdict": "FAIL",
            "reason": "The SQL does not answer the question.",
            "fixed_sql": "",
        },
    )
    monkeypatch.setattr(query_router.llm_service, "repair_sql", _raise_repair_failure)

    response = client.post("/query", json=_payload(seeded_dataset, question="What is average y?"))
    body = response.json()

    assert response.status_code == 200
    assert body["guardian_passed"] is False
    assert body["error"] == "SQL Guardian could not repair semantically incorrect SQL."
    assert any("semantic review FAIL" in line for line in body["guardian_log"])


def test_guardian_blocks_dry_run_fail_when_repair_fails(client, seeded_dataset, monkeypatch):
    monkeypatch.setattr(
        query_router.llm_service,
        "generate_sql",
        lambda *_args, **_kwargs: 'SELECT "missing_col" FROM data',
    )
    monkeypatch.setattr(
        query_router.llm_service,
        "review_sql",
        lambda *_args, **_kwargs: {
            "verdict": "PASS",
            "reason": "Looks aligned.",
            "fixed_sql": "",
        },
    )
    monkeypatch.setattr(query_router.llm_service, "repair_sql", _raise_repair_failure)

    response = client.post("/query", json=_payload(seeded_dataset))
    body = response.json()

    assert response.status_code == 200
    assert body["guardian_passed"] is False
    assert body["error"] == "SQL Guardian could not repair SQL after dry-run failure."
    assert any("dry-run failed" in line for line in body["guardian_log"])


def test_guardian_repairs_and_executes_successfully(client, seeded_dataset, monkeypatch):
    monkeypatch.setattr(
        query_router.llm_service,
        "generate_sql",
        lambda *_args, **_kwargs: 'SELECT "missing_col" FROM data',
    )
    monkeypatch.setattr(
        query_router.llm_service,
        "review_sql",
        lambda *_args, **_kwargs: {
            "verdict": "PASS",
            "reason": "Looks aligned.",
            "fixed_sql": "",
        },
    )
    monkeypatch.setattr(
        query_router.llm_service,
        "repair_sql",
        lambda *_args, **_kwargs: 'SELECT COUNT(*) AS "cnt" FROM data',
    )
    monkeypatch.setattr(
        query_router.llm_service,
        "explain_result",
        lambda *_args, **_kwargs: {
            "explanation": "Query executed successfully.",
            "insights": [],
            "why_analysis": "",
        },
    )

    response = client.post("/query", json=_payload(seeded_dataset, question="How many rows?"))
    body = response.json()

    assert response.status_code == 200
    assert body["error"] is None
    assert body["guardian_enabled"] is True
    assert body["guardian_passed"] is True
    assert body["guardian_retries"] == 1
    assert body["sql"] == 'SELECT COUNT(*) AS "cnt" FROM data'
    assert float(body["result"][0]["cnt"]) == 2.0
    assert any("repaired SQL candidate after dry-run failure" in line for line in body["guardian_log"])


def test_guardian_semantic_fail_accepts_valid_fixed_sql_without_repair(client, seeded_dataset, monkeypatch):
    repair_calls = {"count": 0}
    review_calls = {"count": 0}

    monkeypatch.setattr(
        query_router.llm_service,
        "generate_sql",
        lambda *_args, **_kwargs: 'SELECT "x" FROM data',
    )
    def _review_stub(*_args, **_kwargs):
        review_calls["count"] += 1
        if review_calls["count"] == 1:
            return {
                "verdict": "FAIL",
                "reason": "Wrong aggregation logic.",
                "fixed_sql": 'SELECT AVG("y") AS "avg_y" FROM data',
            }
        return {
            "verdict": "PASS",
            "reason": "Looks aligned after repair.",
            "fixed_sql": "",
        }

    monkeypatch.setattr(query_router.llm_service, "review_sql", _review_stub)

    def _repair_stub(*_args, **_kwargs):
        repair_calls["count"] += 1
        return 'SELECT AVG("y") AS "avg_y" FROM data'

    monkeypatch.setattr(query_router.llm_service, "repair_sql", _repair_stub)
    monkeypatch.setattr(
        query_router.llm_service,
        "explain_result",
        lambda *_args, **_kwargs: {
            "explanation": "Average computed.",
            "insights": [],
            "why_analysis": "",
        },
    )

    response = client.post("/query", json=_payload(seeded_dataset, question="What is average y?"))
    body = response.json()

    assert response.status_code == 200
    assert body["guardian_passed"] is True
    assert body["error"] is None
    assert repair_calls["count"] == 0
    assert body["sql"] == 'SELECT AVG("y") AS "avg_y" FROM data'
    assert any("Verifier supplied a corrected SQL candidate" in line for line in body["guardian_log"])


def test_guardian_semantic_fail_uses_repair_when_fixed_sql_is_unsafe(client, seeded_dataset, monkeypatch):
    repair_calls = {"count": 0, "failed_sql": "", "reason": ""}
    review_calls = {"count": 0}

    monkeypatch.setattr(
        query_router.llm_service,
        "generate_sql",
        lambda *_args, **_kwargs: 'SELECT "x" FROM data',
    )

    def _review_stub(*_args, **_kwargs):
        review_calls["count"] += 1
        if review_calls["count"] == 1:
            return {
                "verdict": "FAIL",
                "reason": "Wrong aggregation logic.",
                "fixed_sql": 'SELECT AVG("y") AS "avg_y" FROM data -- unsafe',
            }
        return {
            "verdict": "PASS",
            "reason": "Looks aligned after repair.",
            "fixed_sql": "",
        }

    monkeypatch.setattr(query_router.llm_service, "review_sql", _review_stub)

    def _repair_stub(*_args, **kwargs):
        repair_calls["count"] += 1
        repair_calls["failed_sql"] = kwargs.get("failed_sql", "")
        repair_calls["reason"] = kwargs.get("error_reason", "")
        return 'SELECT AVG("y") AS "avg_y" FROM data'

    monkeypatch.setattr(query_router.llm_service, "repair_sql", _repair_stub)
    monkeypatch.setattr(
        query_router.llm_service,
        "explain_result",
        lambda *_args, **_kwargs: {
            "explanation": "Average computed.",
            "insights": [],
            "why_analysis": "",
        },
    )

    response = client.post("/query", json=_payload(seeded_dataset, question="What is average y?"))
    body = response.json()

    assert response.status_code == 200
    assert body["guardian_passed"] is True
    assert body["error"] is None
    assert repair_calls["count"] == 1
    assert repair_calls["failed_sql"] == 'SELECT AVG("y") AS "avg_y" FROM data -- unsafe'
    assert "Validation error" in repair_calls["reason"]
    assert body["sql"] == 'SELECT AVG("y") AS "avg_y" FROM data'


def test_sql_validator_allows_safe_replace_function():
    sql = 'SELECT REPLACE("x", "UNKNOWN", "") AS "x_clean" FROM data'
    assert llm_service.validate_sql(sql) == sql


def test_sql_validator_blocks_replace_into_statement():
    with pytest.raises(llm_service.SQLValidationError):
        llm_service.validate_sql('WITH t AS (SELECT 1) SELECT * FROM t; REPLACE INTO data VALUES (1)')
