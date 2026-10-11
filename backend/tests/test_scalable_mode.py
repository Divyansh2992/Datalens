from routers import query as query_router
from services import spark_engine


def test_run_spark_pipeline_falls_back_to_pandas(monkeypatch):
    spark_engine._spark_session = None

    monkeypatch.setattr(
        spark_engine,
        "get_spark_session",
        lambda: (_ for _ in ()).throw(RuntimeError("Python worker failed to connect back")),
    )

    result = spark_engine.run_spark_pipeline(
        spark_engine.pd.DataFrame(
            {
                "x": [1, 2, 2, 2],
                "y": [10, None, 20, 20],
            }
        )
    )

    assert result["execution_mode"] == "pandas-fallback"
    assert "Python worker failed to connect back" in result["fallback_reason"]
    assert result["rows_processed"] == 4
    assert len(result["cleaned_df"]) == 3
    assert result["cleaned_df"]["y"].isna().sum() == 0
    assert float(result["cleaned_df"]["y"].iloc[0]) == 10.0
    assert float(result["cleaned_df"]["y"].iloc[1]) == 16.666666666666668


def test_scalable_query_falls_back_when_spark_worker_startup_fails(client, seeded_dataset, monkeypatch):
    spark_engine._spark_session = None

    monkeypatch.setattr(
        query_router.llm_service,
        "generate_sql",
        lambda *_args, **_kwargs: 'SELECT COUNT(*) AS "cnt" FROM data',
    )
    monkeypatch.setattr(
        query_router.llm_service,
        "explain_result",
        lambda *_args, **_kwargs: {
            "explanation": "Count computed.",
            "insights": [],
            "why_analysis": "",
        },
    )
    monkeypatch.setattr(
        query_router.spark_engine,
        "get_spark_session",
        lambda: (_ for _ in ()).throw(RuntimeError("Python worker failed to connect back")),
    )

    response = client.post(
        "/query",
        json={
            "dataset_id": seeded_dataset,
            "question": "How many rows are there?",
            "mode": "scalable",
            "guardian_enabled": False,
        },
    )
    body = response.json()

    assert response.status_code == 200
    assert body["error"] is None
    assert body["mode"] == "scalable"
    assert body["result"][0]["cnt"] == 2
    assert any("fell back to local pandas preprocessing" in line for line in body["preprocessing_log"])
    assert any("Python worker failed to connect back" in line for line in body["preprocessing_log"])
