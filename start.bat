@echo off
setlocal
echo ==========================================
echo   Talk to Data - Starting all services
echo ==========================================

:: Check for .env
if not exist "backend\.env" (
    echo ERROR: backend\.env not found!
    echo Please copy backend\.env.example to backend\.env
    echo and set your GROQ_API_KEY.
    pause
    exit /b 1
)

:: Detect local Java (for Spark scalable mode)
set "LOCAL_JAVA_HOME="
for /d %%D in ("%~dp0.local-java\jdk-17*") do (
    set "LOCAL_JAVA_HOME=%%~fD"
    goto :java_found
)
for /d %%D in ("%~dp0.local-java\jdk-*") do (
    set "LOCAL_JAVA_HOME=%%~fD"
    goto :java_found
)

:java_found
if defined LOCAL_JAVA_HOME (
    echo INFO: Found local Java runtime for Spark: %LOCAL_JAVA_HOME%
) else (
    echo INFO: No local Java runtime found under .local-java\jdk-*
    echo INFO: Scalable mode may fail unless system Java is 17+
)

echo [1/2] Starting FastAPI backend on port 8000...
if defined LOCAL_JAVA_HOME goto start_backend_with_java
start "Talk-to-Data Backend" cmd /k "cd backend && py -3.12 -m uvicorn main:app --reload --host 0.0.0.0 --port 8000 || uvicorn main:app --reload --host 0.0.0.0 --port 8000"
goto backend_started

:start_backend_with_java
start "Talk-to-Data Backend" cmd /k "cd backend && set \"JAVA_HOME=%LOCAL_JAVA_HOME%\" && set \"PATH=%LOCAL_JAVA_HOME%\bin;%PATH%\" && py -3.12 -m uvicorn main:app --reload --host 0.0.0.0 --port 8000 || uvicorn main:app --reload --host 0.0.0.0 --port 8000"

:backend_started

timeout /t 3 /nobreak >nul

echo [2/2] Starting Vite frontend on port 5173...
start "Talk-to-Data Frontend" cmd /k "cd frontend && npm run dev"

timeout /t 4 /nobreak >nul

echo.
echo ==========================================
echo   Both servers started!
echo   Backend:  http://localhost:8000
echo   Frontend: http://localhost:5173
echo   API Docs: http://localhost:8000/docs
echo ==========================================

start http://localhost:5173
