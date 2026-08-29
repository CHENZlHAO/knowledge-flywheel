@echo off
REM Cross-build the Windows amd64 agent, stamp the version, and optionally sign it.
REM Usage: build-windows.bat [version] [signing-cert-sha1]
setlocal
set VERSION=%1
if "%VERSION%"=="" set VERSION=0.1.0
set CERT_SHA1=%2

cd /d %~dp0..\..
set GOOS=windows
set GOARCH=amd64
go mod tidy
go build -trimpath -ldflags "-s -w -X main.agentVersion=%VERSION%" -o knowledge-edge-agent.exe .

if "%CERT_SHA1%"=="" (
  echo Built knowledge-edge-agent.exe (unsigned). For production, sign with signtool:
  echo   signtool sign /sha1 %CERT_SHA1% /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 knowledge-edge-agent.exe
  exit /b 0
)
signtool sign /sha1 %CERT_SHA1% /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 knowledge-edge-agent.exe
echo Built and signed knowledge-edge-agent.exe
endlocal
