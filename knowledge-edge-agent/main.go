package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	mqtt "github.com/eclipse/paho.mqtt.golang"
)

var safeNodeID = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`)

// agentVersion is overridable at link time for release builds:
//
//	go build -ldflags "-X main.agentVersion=1.2.3"
var agentVersion = "0.1.0"

type Config struct {
	NodeID            string
	CenterURL         string
	NodeAPIKey        string
	WatchDir          string
	AgentVersion      string
	Interval          time.Duration
	CommandPoll       time.Duration
	IsReplica         bool
	SyncthingURL      string
	SyncthingKey      string
	SyncthingFolderID string
	SyncWait          time.Duration
	SyncPoll          time.Duration
	MQTTBroker        string
	MQTTUsername      string
	MQTTPassword      string
	MQTTCAFile        string
	MQTTClientCert    string
	MQTTClientKey     string
	Once              bool
	Service           string
}

type heartbeat struct {
	NodeID       string  `json:"node_id"`
	Hostname     string  `json:"hostname"`
	AgentVersion string  `json:"agent_version"`
	CPUPercent   float64 `json:"cpu_percent"`
	DiskFree     int64   `json:"disk_free_bytes"`
	IsReplica    bool    `json:"is_replica"`
	Status       string  `json:"status,omitempty"`
}

type fileReport struct {
	NodeID   string `json:"node_id"`
	Path     string `json:"path"`
	FileHash string `json:"file_hash"`
	Size     int64  `json:"size_bytes"`
}

type remoteCommand struct {
	ID          int            `json:"id"`
	NodeID      string         `json:"node_id"`
	CommandType string         `json:"command_type"`
	Payload     map[string]any `json:"payload"`
	Status      string         `json:"status"`
}

type commandEnvelope struct {
	Command *remoteCommand `json:"command"`
}

type commandAck struct {
	Status string         `json:"status"`
	Result map[string]any `json:"result"`
	Error  string         `json:"error,omitempty"`
}

func main() {
	cfg := Config{}
	flag.StringVar(&cfg.NodeID, "node-id", "", "stable node identity")
	flag.StringVar(&cfg.CenterURL, "center-url", "http://127.0.0.1:8000", "knowledge hub URL")
	flag.StringVar(&cfg.NodeAPIKey, "node-api-key", "", "knowledge hub node API key")
	flag.StringVar(&cfg.WatchDir, "watch-dir", ".", "directory to report")
	flag.StringVar(&cfg.AgentVersion, "version", agentVersion, "agent version")
	flag.DurationVar(&cfg.Interval, "interval", 30*time.Second, "report interval")
	flag.DurationVar(&cfg.CommandPoll, "command-poll", 15*time.Second, "remote command poll interval; 0 disables polling")
	flag.BoolVar(&cfg.IsReplica, "is-replica", false, "allow this node to serve fixed replica commands")
	flag.StringVar(&cfg.SyncthingURL, "syncthing-url", "", "Syncthing REST API URL; empty disables byte synchronization")
	flag.StringVar(&cfg.SyncthingKey, "syncthing-api-key", "", "Syncthing REST API key")
	flag.StringVar(&cfg.SyncthingFolderID, "syncthing-folder-id", "", "Syncthing folder ID mapped to watch-dir")
	flag.DurationVar(&cfg.SyncWait, "sync-wait", 2*time.Minute, "maximum time to wait for a verified replica")
	flag.DurationVar(&cfg.SyncPoll, "sync-poll", 2*time.Second, "replica verification poll interval")
	flag.StringVar(&cfg.MQTTBroker, "mqtt-broker", "", "MQTT TLS broker URL, for example tls://hub.local:8883; empty disables MQTT status")
	flag.StringVar(&cfg.MQTTUsername, "mqtt-username", "", "per-node MQTT username")
	flag.StringVar(&cfg.MQTTPassword, "mqtt-password", "", "per-node MQTT password")
	flag.StringVar(&cfg.MQTTCAFile, "mqtt-ca-file", "", "PEM CA certificate used to verify the MQTT broker")
	flag.StringVar(&cfg.MQTTClientCert, "mqtt-client-cert", "", "PEM client certificate for mutual TLS")
	flag.StringVar(&cfg.MQTTClientKey, "mqtt-client-key", "", "PEM client private key for mutual TLS")
	flag.BoolVar(&cfg.Once, "once", false, "run one heartbeat/file-report cycle and exit")
	flag.StringVar(&cfg.Service, "service", "", "Windows service command: install | uninstall | run")
	flag.Parse()
	if cfg.NodeID == "" {
		name, err := os.Hostname()
		if err != nil {
			panic(err)
		}
		cfg.NodeID = name
	}
	if cfg.Service != "" {
		if err := handleServiceCommand(cfg); err != nil {
			fmt.Fprintln(os.Stderr, "service:", err)
			os.Exit(1)
		}
		return
	}
	if err := runForeground(cfg); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

// runForeground runs the agent in the foreground (development and macOS use).
func runForeground(cfg Config) error {
	return runAgentLoop(cfg, nil)
}

// runAgentLoop runs one immediate cycle, then repeats on the configured
// interval until stop is closed. A nil stop channel runs forever.
func runAgentLoop(cfg Config, stop <-chan struct{}) error {
	client := &http.Client{Timeout: 10 * time.Second}
	ctx := context.Background()
	mqttClient, err := connectMQTT(cfg)
	if err != nil {
		fmt.Fprintln(os.Stderr, "mqtt:", err)
	} else if mqttClient != nil {
		defer mqttClient.Disconnect(250)
	}
	if err := runOnceWithMQTT(ctx, client, mqttClient, cfg); err != nil {
		fmt.Fprintln(os.Stderr, err)
	}
	if cfg.Once {
		return nil
	}
	interval := cfg.Interval
	if interval <= 0 {
		interval = 30 * time.Second
	}
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	if cfg.CommandPoll > 0 {
		go commandLoop(ctx, client, cfg)
	}
	for {
		select {
		case <-stop:
			return nil
		case <-ticker.C:
			if err := runOnceWithMQTT(ctx, client, mqttClient, cfg); err != nil {
				fmt.Fprintln(os.Stderr, err)
			}
		}
	}
}

func commandLoop(ctx context.Context, client *http.Client, cfg Config) {
	ticker := time.NewTicker(cfg.CommandPoll)
	defer ticker.Stop()
	for range ticker.C {
		if err := pollAndExecuteCommand(ctx, client, cfg); err != nil {
			fmt.Fprintln(os.Stderr, "remote command:", err)
		}
	}
}

func pollAndExecuteCommand(ctx context.Context, client *http.Client, cfg Config) error {
	var envelope commandEnvelope
	if err := getJSON(ctx, client, endpoint(cfg, "/api/v1/nodes/"+cfg.NodeID+"/commands/next"), &envelope, cfg.NodeAPIKey); err != nil {
		return err
	}
	if envelope.Command == nil {
		return nil
	}
	ack := executeRemoteCommandContext(ctx, client, cfg, envelope.Command)
	return postJSON(ctx, client, endpoint(cfg, fmt.Sprintf("/api/v1/nodes/%s/commands/%d/ack", cfg.NodeID, envelope.Command.ID)), ack, cfg.NodeAPIKey)
}

func executeRemoteCommand(cfg Config, command *remoteCommand) commandAck {
	return executeRemoteCommandContext(context.Background(), &http.Client{Timeout: 10 * time.Second}, cfg, command)
}

func executeRemoteCommandContext(ctx context.Context, client *http.Client, cfg Config, command *remoteCommand) commandAck {
	ack := commandAck{Status: "success", Result: map[string]any{"execution": "completed"}}
	switch command.CommandType {
	case "retry_task":
		ack.Result["execution"] = "queued_task_retry"
	case "sync_replica":
		return executeReplicaSync(ctx, client, cfg, command)
	case "restart_agent", "reset_sync":
		ack.Status = "failed"
		ack.Error = "edge execution adapter not installed"
		ack.Result["execution"] = "not_implemented"
	default:
		ack.Status = "failed"
		ack.Error = "unsupported command"
	}
	return ack
}

func executeReplicaSync(ctx context.Context, client *http.Client, cfg Config, command *remoteCommand) commandAck {
	if !cfg.IsReplica {
		return commandAck{Status: "failed", Error: "node is not configured as a replica", Result: map[string]any{"execution": "rejected"}}
	}
	relative, ok := command.Payload["relative_path"].(string)
	wantHash, hashOK := command.Payload["file_hash"].(string)
	if !ok || !hashOK || relative == "" || !validSHA256(wantHash) {
		return commandAck{Status: "failed", Error: "sync command requires relative path and file hash", Result: map[string]any{"execution": "rejected"}}
	}
	clean := filepath.Clean(relative)
	if filepath.IsAbs(relative) || clean == "." || clean == ".." || strings.HasPrefix(clean, ".."+string(filepath.Separator)) {
		return commandAck{Status: "failed", Error: "invalid relative path", Result: map[string]any{"execution": "rejected"}}
	}
	path := filepath.Join(cfg.WatchDir, clean)
	if err := rejectSymlinkPath(cfg.WatchDir, clean); err != nil {
		return commandAck{Status: "failed", Error: err.Error(), Result: map[string]any{"execution": "rejected"}}
	}
	if gotHash, exists, err := hashIfRegular(path); err != nil {
		return commandAck{Status: "failed", Error: fmt.Sprintf("verify replica file: %v", err), Result: map[string]any{"execution": "verify_failed"}}
	} else if exists && gotHash == wantHash {
		return verifiedReplicaAck("verified_existing_replica", gotHash, cfg.SyncthingFolderID)
	} else if (cfg.SyncthingURL == "" || cfg.SyncthingKey == "" || cfg.SyncthingFolderID == "") && exists {
		return commandAck{Status: "failed", Error: "replica file hash mismatch", Result: map[string]any{"execution": "verify_failed", "file_hash": gotHash}}
	}
	if cfg.SyncthingURL == "" || cfg.SyncthingKey == "" || cfg.SyncthingFolderID == "" {
		return commandAck{Status: "failed", Error: "sync adapter not configured; target bytes are not present", Result: map[string]any{"execution": "adapter_missing"}}
	}
	if err := triggerSyncthingScan(ctx, client, cfg, filepath.ToSlash(clean)); err != nil {
		return commandAck{Status: "failed", Error: fmt.Sprintf("trigger Syncthing scan: %v", err), Result: map[string]any{"execution": "sync_failed"}}
	}
	return waitForVerifiedReplica(ctx, cfg, path, wantHash)
}

func validSHA256(value string) bool {
	if len(value) != sha256.Size*2 {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}

func rejectSymlinkPath(root, relative string) error {
	rootAbs, err := filepath.Abs(root)
	if err != nil {
		return fmt.Errorf("resolve watch directory: %w", err)
	}
	current := rootAbs
	for _, part := range strings.Split(filepath.Clean(relative), string(filepath.Separator)) {
		current = filepath.Join(current, part)
		info, err := os.Lstat(current)
		if os.IsNotExist(err) {
			continue
		}
		if err != nil {
			return fmt.Errorf("inspect replica path: %w", err)
		}
		if info.Mode()&os.ModeSymlink != 0 {
			return fmt.Errorf("replica path contains a symbolic link")
		}
	}
	return nil
}

func hashIfRegular(path string) (string, bool, error) {
	info, err := os.Stat(path)
	if os.IsNotExist(err) {
		return "", false, nil
	}
	if err != nil {
		return "", false, err
	}
	if !info.Mode().IsRegular() {
		return "", false, fmt.Errorf("target is not a regular file")
	}
	hash, err := hashFile(path)
	return hash, true, err
}

func triggerSyncthingScan(ctx context.Context, client *http.Client, cfg Config, relative string) error {
	base, err := url.Parse(strings.TrimRight(cfg.SyncthingURL, "/") + "/rest/db/scan")
	if err != nil {
		return err
	}
	query := base.Query()
	query.Set("folder", cfg.SyncthingFolderID)
	query.Set("sub", relative)
	base.RawQuery = query.Encode()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, base.String(), nil)
	if err != nil {
		return err
	}
	req.Header.Set("X-API-Key", cfg.SyncthingKey)
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 1024))
		return fmt.Errorf("Syncthing returned %s: %s", resp.Status, strings.TrimSpace(string(body)))
	}
	return nil
}

func waitForVerifiedReplica(ctx context.Context, cfg Config, path, wantHash string) commandAck {
	wait := cfg.SyncWait
	if wait <= 0 {
		wait = 2 * time.Minute
	}
	poll := cfg.SyncPoll
	if poll <= 0 {
		poll = 2 * time.Second
	}
	deadline := time.NewTimer(wait)
	ticker := time.NewTicker(poll)
	defer deadline.Stop()
	defer ticker.Stop()
	var lastHash string
	for {
		gotHash, exists, err := hashIfRegular(path)
		if err != nil {
			return commandAck{Status: "failed", Error: fmt.Sprintf("verify synchronized replica: %v", err), Result: map[string]any{"execution": "verify_failed"}}
		}
		if exists {
			lastHash = gotHash
			if gotHash == wantHash {
				return verifiedReplicaAck("syncthing_verified_replica", gotHash, cfg.SyncthingFolderID)
			}
		}
		select {
		case <-ctx.Done():
			return commandAck{Status: "failed", Error: "replica synchronization cancelled", Result: map[string]any{"execution": "sync_cancelled"}}
		case <-deadline.C:
			result := map[string]any{"execution": "sync_timeout"}
			if lastHash != "" {
				result["file_hash"] = lastHash
			}
			return commandAck{Status: "failed", Error: "timed out waiting for Syncthing to provide the verified replica", Result: result}
		case <-ticker.C:
		}
	}
}

func verifiedReplicaAck(execution, fileHash, folderID string) commandAck {
	result := map[string]any{"execution": execution, "verified": true, "file_hash": fileHash}
	if folderID != "" {
		result["syncthing_folder_id"] = folderID
	}
	return commandAck{Status: "success", Result: result}
}

func runOnce(ctx context.Context, client *http.Client, cfg Config) error {
	return runOnceWithMQTT(ctx, client, nil, cfg)
}

func runOnceWithMQTT(ctx context.Context, client *http.Client, mqttClient mqtt.Client, cfg Config) error {
	beat := heartbeat{NodeID: cfg.NodeID, Hostname: cfg.NodeID, AgentVersion: cfg.AgentVersion, IsReplica: cfg.IsReplica, Status: "online"}
	if err := postJSON(ctx, client, endpoint(cfg, "/api/v1/nodes/heartbeat"), beat, cfg.NodeAPIKey); err != nil {
		return fmt.Errorf("heartbeat: %w", err)
	}
	if mqttClient != nil && mqttClient.IsConnected() {
		payload, err := json.Marshal(beat)
		if err != nil {
			return fmt.Errorf("encode MQTT heartbeat: %w", err)
		}
		token := mqttClient.Publish(nodeStatusTopic(cfg.NodeID), 1, false, payload)
		if !token.WaitTimeout(5 * time.Second) {
			return fmt.Errorf("publish MQTT heartbeat: timeout")
		}
		if err := token.Error(); err != nil {
			return fmt.Errorf("publish MQTT heartbeat: %w", err)
		}
	}
	return filepath.Walk(cfg.WatchDir, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if info.IsDir() || info.Size() == 0 {
			return nil
		}
		hash, err := hashFile(path)
		if err != nil {
			return err
		}
		relativePath, err := filepath.Rel(cfg.WatchDir, path)
		if err != nil {
			return err
		}
		return postJSON(ctx, client, endpoint(cfg, "/api/v1/files/report"), fileReport{NodeID: cfg.NodeID, Path: filepath.ToSlash(relativePath), FileHash: hash, Size: info.Size()}, cfg.NodeAPIKey)
	})
}

func nodeStatusTopic(nodeID string) string {
	return "knowledge/nodes/" + nodeID + "/status"
}

func connectMQTT(cfg Config) (mqtt.Client, error) {
	if cfg.MQTTBroker == "" {
		return nil, nil
	}
	if !safeNodeID.MatchString(cfg.NodeID) {
		return nil, fmt.Errorf("node ID is unsafe for MQTT topic")
	}
	if !strings.HasPrefix(cfg.MQTTBroker, "tls://") && !strings.HasPrefix(cfg.MQTTBroker, "ssl://") {
		return nil, fmt.Errorf("MQTT broker must use tls:// or ssl://")
	}
	if cfg.MQTTUsername != cfg.NodeID {
		return nil, fmt.Errorf("MQTT username must equal node ID for topic ACL isolation")
	}
	if cfg.MQTTPassword == "" || cfg.MQTTCAFile == "" {
		return nil, fmt.Errorf("MQTT TLS requires per-node password and CA file")
	}
	caPEM, err := os.ReadFile(cfg.MQTTCAFile)
	if err != nil {
		return nil, fmt.Errorf("read MQTT CA: %w", err)
	}
	roots := x509.NewCertPool()
	if !roots.AppendCertsFromPEM(caPEM) {
		return nil, fmt.Errorf("MQTT CA file contains no certificates")
	}
	certificates := []tls.Certificate{}
	if (cfg.MQTTClientCert == "") != (cfg.MQTTClientKey == "") {
		return nil, fmt.Errorf("MQTT client certificate and key must be configured together")
	}
	if cfg.MQTTClientCert != "" {
		clientCertificate, err := tls.LoadX509KeyPair(cfg.MQTTClientCert, cfg.MQTTClientKey)
		if err != nil {
			return nil, fmt.Errorf("load MQTT client certificate: %w", err)
		}
		certificates = append(certificates, clientCertificate)
	}
	will, err := json.Marshal(map[string]any{"node_id": cfg.NodeID, "status": "offline", "reason": "mqtt_will", "reported_at": time.Now().UTC().Format(time.RFC3339)})
	if err != nil {
		return nil, err
	}
	opts := mqtt.NewClientOptions().
		AddBroker(cfg.MQTTBroker).
		SetClientID("knowledge-edge-" + cfg.NodeID).
		SetUsername(cfg.MQTTUsername).
		SetPassword(cfg.MQTTPassword).
		SetTLSConfig(&tls.Config{MinVersion: tls.VersionTLS12, RootCAs: roots, Certificates: certificates}).
		SetCleanSession(false).
		SetAutoReconnect(true).
		SetConnectRetry(true).
		SetConnectRetryInterval(5 * time.Second).
		SetKeepAlive(30 * time.Second).
		SetPingTimeout(10 * time.Second)
	opts.SetWill(nodeStatusTopic(cfg.NodeID), string(will), 1, true)
	client := mqtt.NewClient(opts)
	token := client.Connect()
	if !token.WaitTimeout(15 * time.Second) {
		return nil, fmt.Errorf("connect timeout")
	}
	if err := token.Error(); err != nil {
		return nil, err
	}
	return client, nil
}

func hashFile(path string) (string, error) {
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

func endpoint(cfg Config, path string) string {
	return strings.TrimRight(cfg.CenterURL, "/") + path
}

func postJSON(ctx context.Context, client *http.Client, url string, payload any, nodeAPIKey string) error {
	body, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	if nodeAPIKey != "" {
		req.Header.Set("X-Node-Key", nodeAPIKey)
	}
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		return fmt.Errorf("center returned %s", resp.Status)
	}
	return nil
}

func getJSON(ctx context.Context, client *http.Client, url string, target any, nodeAPIKey string) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return err
	}
	if nodeAPIKey != "" {
		req.Header.Set("X-Node-Key", nodeAPIKey)
	}
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		return fmt.Errorf("center returned %s", resp.Status)
	}
	return json.NewDecoder(resp.Body).Decode(target)
}
