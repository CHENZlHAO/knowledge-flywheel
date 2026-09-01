.PHONY: setup test test-go syntax compose-config acceptance-smoke build-edge build-all build-mac-center deploy-center init-production inspect-production

setup:
	./scripts/setup-python.sh

test:
	cd knowledge-hub && .venv/bin/python -m pytest -q tests

test-go:
	cd knowledge-edge-agent && go mod tidy && go test ./...

syntax:
	cd knowledge-hub && .venv/bin/python -m compileall -q app tests

compose-config:
	cd knowledge-hub && docker compose config >/dev/null

acceptance-smoke:
	python3 scripts/acceptance-smoke.py

build-edge:
	cd knowledge-edge-agent && CGO_ENABLED=0 GOOS=windows GOARCH=amd64 go build -trimpath -ldflags "-s -w -X main.agentVersion=$(VERSION)" -o dist/knowledge-edge-agent.exe .

build-all:
	./scripts/build-all.sh $(VERSION)

build-mac-center:
	./scripts/build-mac-center.sh

deploy-center:
	./scripts/deploy-center.sh

init-production:
	./scripts/init-production-env.sh

inspect-production:
	./scripts/inspect-production.sh

VERSION ?= 0.2.0
