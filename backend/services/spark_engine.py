"""
Spark Engine (Scalable Mode)

Optional local PySpark preprocessing pipeline used when query mode is "scalable".
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict

import pandas as pd

_spark_session = None


def get_spark_session():
    """Create (or reuse) a local SparkSession."""
    global _spark_session

    if _spark_session is not None:
        return _spark_session

    _configure_local_spark_environment()

    try:
        from pyspark.sql import SparkSession
    except Exception as exc:
        raise RuntimeError(
            "PySpark is not available. Install 'pyspark' to use scalable mode."
        ) from exc

    _spark_session = (
        SparkSession.builder
        .appName("talk-to-data-scalable")
        .master("local[1]")   # ⚠️ IMPORTANT: not [*]
        .config("spark.ui.enabled", "false")
        .config("spark.local.ip", "127.0.0.1")
        .config("spark.driver.bindAddress", "127.0.0.1")
        .config("spark.driver.host", "127.0.0.1")
        .config("spark.driver.port", "0")
        .config("spark.blockManager.port", "0")
        .config("spark.executorEnv.PYSPARK_PYTHON", os.environ["PYSPARK_PYTHON"])
        .config("spark.executorEnv.PYSPARK_DRIVER_PYTHON", os.environ["PYSPARK_DRIVER_PYTHON"])
        .config("spark.sql.shuffle.partitions", "4")  # reduce load
        .config("spark.default.parallelism", "4")
        .getOrCreate()
    )
    
    _spark_session.sparkContext.setLogLevel("WARN")
    return _spark_session


def _configure_local_spark_environment() -> None:
    """Force loopback networking and the active Python executable for local Spark."""
    python_exec = sys.executable or "python"
    os.environ.setdefault("PYSPARK_PYTHON", python_exec)
    os.environ.setdefault("PYSPARK_DRIVER_PYTHON", python_exec)
    os.environ.setdefault("SPARK_LOCAL_IP", "127.0.0.1")
    os.environ.setdefault("SPARK_LOCAL_HOSTNAME", "localhost")


def _get_numeric_columns(spark_df) -> list[str]:
    from pyspark.sql import types as T

    numeric_types = (
        T.ByteType,
        T.ShortType,
        T.IntegerType,
        T.LongType,
        T.FloatType,
        T.DoubleType,
        T.DecimalType,
    )

    return [
        field.name
        for field in spark_df.schema.fields
        if isinstance(field.dataType, numeric_types)
    ]


def run_spark_pipeline(df: pd.DataFrame) -> Dict[str, Any]:
    """
    Run a simple local Spark cleaning pipeline.

    Steps:
    1. pandas -> Spark DataFrame
    2. Numeric null imputation using mean
    3. Remove duplicates
    4. Approximate percentile-based outlier filtering (IQR style)
    5. Spark DataFrame -> pandas
    """
    if df is None:
        raise ValueError("Input DataFrame cannot be None.")

    if df.empty or df.shape[1] == 0:
        return {
            "cleaned_df": df.copy(),
            "rows_processed": int(df.shape[0]),
            "execution_mode": "spark",
        }

    try:
        spark = get_spark_session()
        return _run_pyspark_pipeline(df, spark)
    except Exception as exc:
        fallback = _run_pandas_fallback(df)
        fallback["execution_mode"] = "pandas-fallback"
        fallback["fallback_reason"] = str(exc)
        return fallback


def _run_pyspark_pipeline(df: pd.DataFrame, spark) -> Dict[str, Any]:
    from pyspark.sql import functions as F

    spark_df = spark.createDataFrame(df)
    numeric_cols = _get_numeric_columns(spark_df)

    if numeric_cols:
        mean_exprs = [F.mean(F.col(col)).alias(col) for col in numeric_cols]
        mean_row = spark_df.agg(*mean_exprs).first()

        if mean_row is not None:
            fill_values = {
                col: mean_row[col]
                for col in numeric_cols
                if mean_row[col] is not None
            }
            if fill_values:
                spark_df = spark_df.na.fill(fill_values)

    spark_df = spark_df.dropDuplicates()

    outlier_filter = None
    for col in numeric_cols:
        try:
            q1, q3 = spark_df.approxQuantile(col, [0.25, 0.75], 0.05)
        except Exception:
            continue

        iqr = q3 - q1
        lower = q1 - (1.5 * iqr)
        upper = q3 + (1.5 * iqr)

        col_filter = (F.col(col) >= lower) & (F.col(col) <= upper)
        outlier_filter = col_filter if outlier_filter is None else (outlier_filter & col_filter)

    if outlier_filter is not None:
        spark_df = spark_df.filter(outlier_filter)

    cleaned_df = spark_df.toPandas()

    return {
        "cleaned_df": cleaned_df,
        "rows_processed": int(df.shape[0]),
        "execution_mode": "spark",
    }


def _run_pandas_fallback(df: pd.DataFrame) -> Dict[str, Any]:
    """
    Fallback path when local Spark worker startup fails. It mirrors the basic
    Spark cleaning steps so scalable mode remains usable.
    """
    cleaned_df = df.copy()
    numeric_cols = cleaned_df.select_dtypes(include=["number"]).columns.tolist()

    if numeric_cols:
        means = cleaned_df[numeric_cols].mean(numeric_only=True)
        cleaned_df[numeric_cols] = cleaned_df[numeric_cols].fillna(means)

    cleaned_df = cleaned_df.drop_duplicates()

    for col in numeric_cols:
        series = cleaned_df[col]
        if series.empty:
            continue
        q1 = series.quantile(0.25)
        q3 = series.quantile(0.75)
        if pd.isna(q1) or pd.isna(q3):
            continue
        iqr = q3 - q1
        lower = q1 - (1.5 * iqr)
        upper = q3 + (1.5 * iqr)
        cleaned_df = cleaned_df[(cleaned_df[col] >= lower) & (cleaned_df[col] <= upper)]

    return {
        "cleaned_df": cleaned_df.reset_index(drop=True),
        "rows_processed": int(df.shape[0]),
    }
