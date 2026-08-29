@echo off
REM Install the Knowledge Edge Agent as a Windows Service (run as Administrator).
REM The agent must be built with: go build -o knowledge-edge-agent.exe .
setlocal
set AGENT_EXE=%~dp0..\..\knowledge-edge-agent.exe
if not exist "%AGENT_EXE%" (
  echo Agent executable not found: %AGENT_EXE%
  echo Run scripts\windows\build-windows.bat first.
  exit /b 1
)
sc.exe stop KnowledgeEdgeAgent >nul 2>&1
sc.exe delete KnowledgeEdgeAgent >nul 2>&1
sc.exe create KnowledgeEdgeAgent binPath= "\"%AGENT_EXE%\" -service run" start= auto DisplayName= "Knowledge Edge Agent"
if errorlevel 1 exit /b 1
sc.exe description KnowledgeEdgeAgent "Knowledge Flywheel edge agent: file hashing, heartbeat, and fixed-replica sync"
sc.exe start KnowledgeEdgeAgent
echo Service installed and started.
endlocal
