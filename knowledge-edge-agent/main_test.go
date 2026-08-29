package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestPollAndExecuteUnsupportedCommand(t *testing.T) {
	var ack commandAck
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodGet {
			_ = json.NewEncoder(w).Encode(commandEnvelope{Command: &remoteCommand{ID: 7, NodeID: "n1", CommandType: "restart_agent"}})
			return
		}
		if err := json.NewDecoder(r.Body).Decode(&ack); err != nil {
			t.Fatal(err)
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()
	err := pollAndExecuteCommand(context.Background(), server.Client(), Config{NodeID: "n1", CenterURL: server.URL})
	if err != nil {
		t.Fatal(err)
	}
	if ack.Status != "failed" || ack.Error == "" {
		t.Fatalf("ack = %+v", ack)
	}
}

func TestPollAndExecuteSendsNodeAPIKeyAndTrimsCenterURL(t *testing.T) {
	var gotKey string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotKey = r.Header.Get("X-Node-Key")
		if r.Method == http.MethodGet {
			_ = json.NewEncoder(w).Encode(commandEnvelope{})
		}
	}))
	defer server.Close()
	if err := pollAndExecuteCommand(context.Background(), server.Client(), Config{NodeID: "n1", CenterURL: server.URL + "/", NodeAPIKey: "secret"}); err != nil {
		t.Fatal(err)
	}
	if gotKey != "secret" {
		t.Fatalf("node key = %q", gotKey)
	}
}

func TestHashFile(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "a.txt")
	if err := os.WriteFile(path, []byte("hello"), 0o600); err != nil {
		t.Fatal(err)
	}
	got, err := hashFile(path)
	if err != nil {
		t.Fatal(err)
	}
	const want = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
	if got != want {
		t.Fatalf("hash = %s, want %s", got, want)
	}
}

func TestRunOnceReportsRelativePathAndReplicaCapability(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "docs", "a.txt")
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("hello"), 0o600); err != nil {
		t.Fatal(err)
	}
	var gotHeartbeat heartbeat
	var gotReport fileReport
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/v1/nodes/heartbeat":
			_ = json.NewDecoder(r.Body).Decode(&gotHeartbeat)
		case "/api/v1/files/report":
			_ = json.NewDecoder(r.Body).Decode(&gotReport)
		default:
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	if err := runOnce(context.Background(), server.Client(), Config{NodeID: "replica", CenterURL: server.URL, WatchDir: dir, IsReplica: true}); err != nil {
		t.Fatal(err)
	}
	if !gotHeartbeat.IsReplica {
		t.Fatal("replica capability not reported")
	}
	if gotReport.Path != "docs/a.txt" {
		t.Fatalf("path = %q", gotReport.Path)
	}
}

func TestSyncReplicaRejectsPathTraversal(t *testing.T) {
	ack := executeRemoteCommand(Config{NodeID: "replica", WatchDir: t.TempDir(), IsReplica: true}, &remoteCommand{
		CommandType: "sync_replica",
		Payload:     map[string]any{"relative_path": "../outside.txt", "file_hash": strings.Repeat("a", 64)},
	})
	if ack.Status != "failed" || !strings.Contains(ack.Error, "relative path") {
		t.Fatalf("ack = %+v", ack)
	}
}

func TestSyncReplicaVerifiesExistingFileBeforeSuccess(t *testing.T) {
	dir := t.TempDir()
	content := []byte("replica bytes")
	path := filepath.Join(dir, "docs", "file.txt")
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, content, 0o600); err != nil {
		t.Fatal(err)
	}
	wantHash := sha256.Sum256(content)
	ack := executeRemoteCommand(Config{NodeID: "replica", WatchDir: dir, IsReplica: true}, &remoteCommand{
		CommandType: "sync_replica",
		Payload:     map[string]any{"relative_path": "docs/file.txt", "file_hash": hex.EncodeToString(wantHash[:])},
	})
	if ack.Status != "success" || ack.Result["verified"] != true {
		t.Fatalf("ack = %+v", ack)
	}
}

func TestSyncReplicaDoesNotClaimSuccessWithoutAdapter(t *testing.T) {
	ack := executeRemoteCommand(Config{NodeID: "replica", WatchDir: t.TempDir(), IsReplica: true}, &remoteCommand{
		CommandType: "sync_replica",
		Payload:     map[string]any{"relative_path": "docs/missing.txt", "file_hash": strings.Repeat("b", 64)},
	})
	if ack.Status != "failed" || !strings.Contains(ack.Error, "adapter not configured") {
		t.Fatalf("ack = %+v", ack)
	}
}

func TestSyncReplicaTriggersSyncthingAndWaitsForVerifiedBytes(t *testing.T) {
	dir := t.TempDir()
	content := []byte("bytes delivered by syncthing")
	wantHash := sha256.Sum256(content)
	target := filepath.Join(dir, "docs", "synced.txt")
	var gotAPIKey string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAPIKey = r.Header.Get("X-API-Key")
		if r.Method != http.MethodPost || r.URL.Path != "/rest/db/scan" {
			t.Fatalf("unexpected request: %s %s", r.Method, r.URL.Path)
		}
		if r.URL.Query().Get("folder") != "replica-folder" || r.URL.Query().Get("sub") != "docs/synced.txt" {
			t.Fatalf("unexpected query: %s", r.URL.RawQuery)
		}
		if err := os.MkdirAll(filepath.Dir(target), 0o700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(target, content, 0o600); err != nil {
			t.Fatal(err)
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	ack := executeRemoteCommandContext(context.Background(), server.Client(), Config{
		NodeID: "replica", WatchDir: dir, IsReplica: true,
		SyncthingURL: server.URL, SyncthingKey: "syncthing-secret", SyncthingFolderID: "replica-folder",
		SyncWait: time.Second, SyncPoll: time.Millisecond,
	}, &remoteCommand{
		CommandType: "sync_replica",
		Payload:     map[string]any{"relative_path": "docs/synced.txt", "file_hash": hex.EncodeToString(wantHash[:])},
	})
	if ack.Status != "success" || ack.Result["verified"] != true || ack.Result["execution"] != "syncthing_verified_replica" {
		t.Fatalf("ack = %+v", ack)
	}
	if gotAPIKey != "syncthing-secret" {
		t.Fatalf("Syncthing API key = %q", gotAPIKey)
	}
}

func TestSyncReplicaDoesNotClaimSuccessWhenSyncthingFails(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "folder unavailable", http.StatusServiceUnavailable)
	}))
	defer server.Close()
	ack := executeRemoteCommandContext(context.Background(), server.Client(), Config{
		NodeID: "replica", WatchDir: t.TempDir(), IsReplica: true,
		SyncthingURL: server.URL, SyncthingKey: "secret", SyncthingFolderID: "replica-folder",
	}, &remoteCommand{
		CommandType: "sync_replica",
		Payload:     map[string]any{"relative_path": "docs/missing.txt", "file_hash": strings.Repeat("b", 64)},
	})
	if ack.Status != "failed" || ack.Result["execution"] != "sync_failed" {
		t.Fatalf("ack = %+v", ack)
	}
}

func TestSyncReplicaTimesOutWithoutVerifiedBytes(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()
	ack := executeRemoteCommandContext(context.Background(), server.Client(), Config{
		NodeID: "replica", WatchDir: t.TempDir(), IsReplica: true,
		SyncthingURL: server.URL, SyncthingKey: "secret", SyncthingFolderID: "replica-folder",
		SyncWait: 10 * time.Millisecond, SyncPoll: time.Millisecond,
	}, &remoteCommand{
		CommandType: "sync_replica",
		Payload:     map[string]any{"relative_path": "docs/missing.txt", "file_hash": strings.Repeat("b", 64)},
	})
	if ack.Status != "failed" || ack.Result["execution"] != "sync_timeout" {
		t.Fatalf("ack = %+v", ack)
	}
}

func TestSyncReplicaRejectsSymlinkedTargetPath(t *testing.T) {
	dir := t.TempDir()
	outside := t.TempDir()
	if err := os.Symlink(outside, filepath.Join(dir, "linked")); err != nil {
		t.Skipf("symlink unavailable: %v", err)
	}
	ack := executeRemoteCommand(Config{
		NodeID: "replica", WatchDir: dir, IsReplica: true,
		SyncthingURL: "http://127.0.0.1:1", SyncthingKey: "secret", SyncthingFolderID: "replica-folder",
	}, &remoteCommand{
		CommandType: "sync_replica",
		Payload:     map[string]any{"relative_path": "linked/file.txt", "file_hash": strings.Repeat("b", 64)},
	})
	if ack.Status != "failed" || !strings.Contains(ack.Error, "symbolic link") {
		t.Fatalf("ack = %+v", ack)
	}
}

func TestRunAgentLoopOnceModeReturns(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()
	stop := make(chan struct{})
	done := make(chan error, 1)
	go func() {
		done <- runAgentLoop(Config{NodeID: "once-node", CenterURL: server.URL, WatchDir: t.TempDir(), Once: true}, stop)
	}()
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("runAgentLoop did not return in once mode")
	}
}

func TestConnectMQTTRejectsPlaintextBroker(t *testing.T) {
	client, err := connectMQTT(Config{NodeID: "node-1", MQTTBroker: "tcp://broker:1883"})
	if client != nil || err == nil || !strings.Contains(err.Error(), "must use tls") {
		t.Fatalf("client=%v err=%v", client, err)
	}
}

func TestConnectMQTTRejectsUnsafeTopicNodeID(t *testing.T) {
	client, err := connectMQTT(Config{NodeID: "../node", MQTTBroker: "tls://broker:8883"})
	if client != nil || err == nil || !strings.Contains(err.Error(), "unsafe") {
		t.Fatalf("client=%v err=%v", client, err)
	}
}

func TestConnectMQTTRequiresUsernameToMatchNodeID(t *testing.T) {
	client, err := connectMQTT(Config{NodeID: "node-1", MQTTBroker: "tls://broker:8883", MQTTUsername: "node-2"})
	if client != nil || err == nil || !strings.Contains(err.Error(), "must equal node ID") {
		t.Fatalf("client=%v err=%v", client, err)
	}
}
