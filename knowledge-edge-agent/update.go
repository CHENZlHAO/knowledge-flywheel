package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

const latestURL = "https://api.github.com/repos/CHENZlHAO/knowledge-flywheel/releases/latest"

var updateHTTPClient = &http.Client{Timeout: 5 * time.Minute}

type githubAsset struct {
	Name               string `json:"name"`
	BrowserDownloadURL string `json:"browser_download_url"`
	Digest             string `json:"digest"`
}

type githubRelease struct {
	TagName     string        `json:"tag_name"`
	PublishedAt string        `json:"published_at"`
	Assets      []githubAsset `json:"assets"`
}

func edgeAssetName() string {
	ext := ""
	if runtime.GOOS == "windows" {
		ext = ".exe"
	}
	return fmt.Sprintf("knowledge-edge-agent-%s-%s%s", runtime.GOOS, runtime.GOARCH, ext)
}

func fetchLatestRelease(client *http.Client) (*githubRelease, error) {
	req, err := http.NewRequest(http.MethodGet, latestURL, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("User-Agent", "knowledge-edge-agent-updater")
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return nil, fmt.Errorf("GitHub 返回 HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}
	var rel githubRelease
	if err := json.NewDecoder(resp.Body).Decode(&rel); err != nil {
		return nil, err
	}
	return &rel, nil
}

func (w *webServer) updateCheck() map[string]any {
	out := map[string]any{"ok": false, "current_version": agentVersion}
	rel, err := fetchLatestRelease(updateHTTPClient)
	if err != nil {
		out["error"] = err.Error()
		return out
	}
	want := edgeAssetName()
	var asset *githubAsset
	for i := range rel.Assets {
		if rel.Assets[i].Name == want {
			asset = &rel.Assets[i]
			break
		}
	}
	out["ok"] = true
	out["latest_version"] = rel.TagName
	out["published_at"] = rel.PublishedAt
	out["platform_asset"] = want
	out["update_available"] = asset != nil && rel.TagName != "" && rel.TagName != agentVersion
	return out
}

func downloadFile(client *http.Client, url, dest string) error {
	req, err := http.NewRequest(http.MethodGet, url, nil)
	if err != nil {
		return err
	}
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		return fmt.Errorf("下载返回 HTTP %d", resp.StatusCode)
	}
	f, err := os.Create(dest)
	if err != nil {
		return err
	}
	defer f.Close()
	_, err = io.Copy(f, resp.Body)
	return err
}

func sha256File(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

func writeSwapScript(old, new string, pid int, args []string) (string, error) {
	dir := os.TempDir()
	var script string
	if runtime.GOOS == "windows" {
		script = filepath.Join(dir, "knowledge-edge-agent-update.ps1")
		quoted := make([]string, 0, len(args))
		for _, a := range args {
			quoted = append(quoted, "'"+strings.ReplaceAll(a, "'", "''")+"'")
		}
		body := strings.Join([]string{
			fmt.Sprintf("$pidToStop = %d", pid),
			fmt.Sprintf("$old = '%s'", old),
			fmt.Sprintf("$new = '%s'", new),
			fmt.Sprintf("$startArgs = \"%s\"", strings.Join(quoted, " ")),
			"Start-Sleep -Seconds 3",
			"if (Get-Process -Id $pidToStop -ErrorAction SilentlyContinue) { Stop-Process -Id $pidToStop -Force -ErrorAction SilentlyContinue }",
			"Start-Sleep -Seconds 1",
			"if (Test-Path \"$old.prev\") { Remove-Item \"$old.prev\" -Force }",
			"if (Test-Path $old) { Move-Item $old \"$old.prev\" -Force }",
			"Move-Item $new $old -Force",
			"if ($startArgs) { Start-Process -FilePath $old -ArgumentList $startArgs } else { Start-Process -FilePath $old }",
		}, "\r\n")
		if err := os.WriteFile(script, []byte(body), 0o644); err != nil {
			return "", err
		}
	} else {
		script = filepath.Join(dir, "knowledge-edge-agent-update.sh")
		quoted := make([]string, 0, len(args))
		for _, a := range args {
			quoted = append(quoted, "'"+strings.ReplaceAll(a, "'", "'\\''")+"'")
		}
		body := strings.Join([]string{
			"#!/bin/sh",
			"set -eu",
			fmt.Sprintf("OLD='%s'", old),
			fmt.Sprintf("NEW='%s'", new),
			fmt.Sprintf("PID=%d", pid),
			fmt.Sprintf("ARGS=%s", strings.Join(quoted, " ")),
			"sleep 2",
			"kill $PID 2>/dev/null || true",
			"sleep 1",
			"if [ -f \"$OLD.prev\" ]; then rm -f \"$OLD.prev\"; fi",
			"if [ -f \"$OLD\" ]; then mv \"$OLD\" \"$OLD.prev\" 2>/dev/null || true; fi",
			"mv \"$NEW\" \"$OLD\"",
			"chmod +x \"$OLD\"",
			"exec \"$OLD\" $ARGS",
		}, "\n")
		if err := os.WriteFile(script, []byte(body), 0o755); err != nil {
			return "", err
		}
	}
	return script, nil
}

func spawnDetached(script string) error {
	if runtime.GOOS == "windows" {
		cmd := exec.Command("powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script)
		cmd.Stdout = nil
		cmd.Stderr = nil
		return cmd.Start()
	}
	cmd := exec.Command("sh", script)
	cmd.Stdout = nil
	cmd.Stderr = nil
	return cmd.Start()
}

func (w *webServer) updateApply() map[string]any {
	info := w.updateCheck()
	if !boolValue(info["ok"]) {
		return map[string]any{"ok": false, "error": info["error"]}
	}
	if !boolValue(info["update_available"]) {
		return map[string]any{"ok": true, "message": "已是最新版本", "latest_version": info["latest_version"]}
	}
	exe, err := os.Executable()
	if err != nil {
		return map[string]any{"ok": false, "error": err.Error()}
	}
	next := exe + ".next"
	// Re-fetch to get asset URL (updateCheck only returns names we need; fetch full release here).
	rel, err := fetchLatestRelease(updateHTTPClient)
	if err != nil {
		return map[string]any{"ok": false, "error": err.Error()}
	}
	want := edgeAssetName()
	var asset *githubAsset
	for i := range rel.Assets {
		if rel.Assets[i].Name == want {
			asset = &rel.Assets[i]
			break
		}
	}
	if asset == nil {
		return map[string]any{"ok": false, "error": "当前平台没有可下载的边缘端二进制"}
	}
	if err := downloadFile(updateHTTPClient, asset.BrowserDownloadURL, next); err != nil {
		return map[string]any{"ok": false, "error": "下载失败: " + err.Error()}
	}
	if digest := strings.TrimPrefix(asset.Digest, "sha256:"); digest != "" {
		sum, err := sha256File(next)
		if err != nil {
			os.Remove(next)
			return map[string]any{"ok": false, "error": "校验失败: " + err.Error()}
		}
		if sum != digest {
			os.Remove(next)
			return map[string]any{"ok": false, "error": "SHA-256 校验失败，已中止"}
		}
	}
	script, err := writeSwapScript(exe, next, os.Getpid(), os.Args[1:])
	if err != nil {
		os.Remove(next)
		return map[string]any{"ok": false, "error": err.Error()}
	}
	if err := spawnDetached(script); err != nil {
		os.Remove(next)
		return map[string]any{"ok": false, "error": err.Error()}
	}
	return map[string]any{"ok": true, "message": "已下载并校验，即将重启", "latest_version": rel.TagName}
}

func boolValue(v any) bool {
	b, _ := v.(bool)
	return b
}

func (w *webServer) handleUpdateCheck(rw http.ResponseWriter, r *http.Request) {
	if !w.authorized(r) {
		writeJSON(rw, http.StatusUnauthorized, map[string]any{"error": "unauthorized"})
		return
	}
	writeJSON(rw, http.StatusOK, w.updateCheck())
}

func (w *webServer) handleUpdateApply(rw http.ResponseWriter, r *http.Request) {
	if !w.authorized(r) {
		writeJSON(rw, http.StatusUnauthorized, map[string]any{"error": "unauthorized"})
		return
	}
	if r.Method != http.MethodPost {
		writeJSON(rw, http.StatusMethodNotAllowed, map[string]any{"error": "POST only"})
		return
	}
	writeJSON(rw, http.StatusOK, w.updateApply())
}
