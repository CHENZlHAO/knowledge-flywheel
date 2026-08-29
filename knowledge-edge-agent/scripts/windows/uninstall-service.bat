@echo off
REM Stop and remove the Knowledge Edge Agent Windows Service (run as Administrator).
sc.exe stop KnowledgeEdgeAgent >nul 2>&1
sc.exe delete KnowledgeEdgeAgent
if errorlevel 1 exit /b 1
echo Service removed.
