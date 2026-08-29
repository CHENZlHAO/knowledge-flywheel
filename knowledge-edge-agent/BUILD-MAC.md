# Go toolchain on macOS

The edge agent is a separate Go project and does not use the Python virtual environment.

## Recommended: Homebrew

```bash
brew update
brew install go
go version
```

This is the easiest option for a Mac development machine. Homebrew installs both `go` and `gofmt` and keeps upgrades manageable.

## Official installer

1. Open [go.dev/dl](https://go.dev/dl/).
2. Download the macOS installer matching the Mac CPU: `darwin-arm64` for Apple Silicon or `darwin-amd64` for Intel.
3. Run the `.pkg` installer.
4. Open a new terminal and verify:

```bash
go version
gofmt -w knowledge-edge-agent/main.go knowledge-edge-agent/main_test.go
cd knowledge-edge-agent
go test ./...
```

## Version manager (optional)

Use `mise` or `asdf` if multiple projects require different Go versions. Keep the repository's `go.mod` as the source of truth; it currently requires Go 1.22 or newer.

## Build the Windows client from Mac

```bash
cd knowledge-edge-agent
gofmt -w main.go main_test.go
go mod tidy
go test ./...
CGO_ENABLED=0 GOOS=windows GOARCH=amd64 go build -trimpath -ldflags='-s -w' -o dist/knowledge-edge-agent.exe .
```

For ARM64 Windows devices, use `GOARCH=arm64`. Production releases should additionally code-sign the executable and publish checksums.
