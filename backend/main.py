"""
FastAPI application entry point.
Run with: uvicorn main:app --reload --host 0.0.0.0 --port 8000
"""
import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()

from routers import upload, query, data_health_router, auto_visualize, correlation, jobs

app = FastAPI(
    title="Talk to Data API",
    description="AI-powered CSV querying system using DuckDB + Groq LLM",
    version="1.0.0",
)

# ── CORS ───────────────────────────────────────────────────────────────────────
# Always allow local frontends (any port on localhost / 127.0.0.1). In production,
# also allow the deployed frontend URL plus any origins passed via the
# FRONTEND_URL env var on Render.
_local_origin_regex = r"https?://(localhost|127\.0\.0\.1)(:\d+)?"
_allowed_origins = [
    "https://melodic-melba-1010c7.netlify.app",  # deployed frontend
]

# Allow overriding / adding more origins via env var (comma-separated)
_extra = os.environ.get("FRONTEND_URL", "")
if _extra:
    for _url in _extra.split(","):
        _url = _url.strip().rstrip("/")
        if _url and _url not in _allowed_origins:
            _allowed_origins.append(_url)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_origin_regex=_local_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(upload.router, tags=["Upload"])
app.include_router(query.router, tags=["Query"])
app.include_router(data_health_router.router, tags=["Data Health"])
app.include_router(auto_visualize.router, tags=["Auto Visualize"])
app.include_router(correlation.router, tags=["Correlation"])
app.include_router(jobs.router, tags=["Jobs"])


@app.get("/", tags=["Health"])
async def root():
    return {"status": "ok", "message": "Talk to Data API is running."}


@app.get("/health", tags=["Health"])
async def health():
    return {"status": "healthy"}
