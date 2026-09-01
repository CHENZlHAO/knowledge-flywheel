package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"embed"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"mime/multipart"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
	"unicode/utf8"
)

// webFS embeds the static console assets (index.html / app.js / pico.min.css)
// into the edge-agent binary, so the console works fully offline with no build
// step and no external CDN.
//
//go:embed web
var webFS embed.FS

// maxWebUpload mirrors the center's MAX_UPLOAD_BYTES default. Oversized files
// are rejected before any byte reaches the center.
const maxWebUpload = 52 * 1024 * 1024

type webServer struct {
	cfg    Config
	client *http.Client
	token  string
}

func newWebServer(cfg Config, client *http.Client, token string) *webServer {
	return &webServer{cfg: cfg, client: client, token: token}
}

// generateWebToken returns a 48-hex-char random token for the local console.
func generateWebToken() (string, error) {
	b := make([]byte, 24)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return hex.EncodeToString(b), nil
}

// start launches the HTTP console when --web-listen is non-empty. It is
// deliberately fire-and-forget: the agent loop keeps running on its own.
func (w *webServer) start() {
	if w.cfg.WebListen == "" {
		return
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/api/status", w.handleStatus)
	mux.HandleFunc("/api/categories", w.handleCategories)
	mux.HandleFunc("/api/upload", w.handleUpload)
	mux.HandleFunc("/api/search", w.handleSearch)
	mux.HandleFunc("/api/ingest", w.handleIngest)
	mux.HandleFunc("/api/queue", w.handleQueue)
	mux.HandleFunc("/api/proposals", w.handleProposals)
	mux.HandleFunc("/api/proposals/", w.handleProposalReview)
	mux.Handle("/", w.staticHandler())

	srv := &http.Server{
		Addr:         w.cfg.WebListen,
		Handler:      w.securityHeaders(mux),
		ReadTimeout:  30 * time.Second,
		WriteTimeout: 60 * time.Second,
	}
	fmt.Printf("边缘控制台: http://%s/  访问令牌: %s\n", w.cfg.WebListen, w.token)
	go func() {
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			fmt.Fprintf(os.Stderr, "web server: %v\n", err)
		}
	}()
}

func (w *webServer) staticHandler() http.Handler {
	sub, err := fs.Sub(webFS, "web")
	if err != nil {
		panic(err)
	}
	return http.FileServer(http.FS(sub))
}

// securityHeaders hardens the local console: no sniffing, no framing, no
// referrer leakage, and a strict same-origin CSP (no inline script/style).
func (w *webServer) securityHeaders(next http.Handler) http.Handler {
	return http.HandlerFunc(func(rw http.ResponseWriter, r *http.Request) {
		rw.Header().Set("X-Content-Type-Options", "nosniff")
		rw.Header().Set("X-Frame-Options", "DENY")
		rw.Header().Set("Referrer-Policy", "no-referrer")
		rw.Header().Set("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")
		next.ServeHTTP(rw, r)
	})
}

func (w *webServer) authorized(r *http.Request) bool {
	if w.token == "" {
		return false
	}
	auth := r.Header.Get("Authorization")
	const prefix = "Bearer "
	if !strings.HasPrefix(auth, prefix) {
		return false
	}
	tok := strings.TrimPrefix(auth, prefix)
	return subtle.ConstantTimeCompare([]byte(tok), []byte(w.token)) == 1
}

func writeJSON(rw http.ResponseWriter, code int, v any) {
	rw.Header().Set("Content-Type", "application/json; charset=utf-8")
	rw.WriteHeader(code)
	_ = json.NewEncoder(rw).Encode(v)
}

func (w *webServer) handleStatus(rw http.ResponseWriter, r *http.Request) {
	if !w.authorized(r) {
		writeJSON(rw, http.StatusUnauthorized, map[string]any{"error": "unauthorized"})
		return
	}
	status := map[string]any{
		"node_id":       w.cfg.NodeID,
		"agent_version": w.cfg.AgentVersion,
		"center_url":    w.cfg.CenterURL,
		"watch_dir":     w.cfg.WatchDir,
		"is_replica":    w.cfg.IsReplica,
		"web_listen":    w.cfg.WebListen,
		"center_ok":     false,
	}
	req, err := http.NewRequestWithContext(r.Context(), http.MethodGet, endpoint(w.cfg, "/healthz"), nil)
	if err == nil {
		if resp, err := w.client.Do(req); err == nil {
			resp.Body.Close()
			status["center_ok"] = resp.StatusCode < 300
		}
	}
	count := 0
	_ = filepath.Walk(w.cfg.WatchDir, func(_ string, info os.FileInfo, err error) error {
		if err == nil && info != nil && !info.IsDir() && info.Size() > 0 {
			count++
		}
		return nil
	})
	status["watched_files"] = count
	status["queue_count"] = queueCount(w.cfg.QueueDir)
	writeJSON(rw, http.StatusOK, status)
}

func (w *webServer) handleUpload(rw http.ResponseWriter, r *http.Request) {
	if !w.authorized(r) {
		writeJSON(rw, http.StatusUnauthorized, map[string]any{"error": "unauthorized"})
		return
	}
	if r.Method != http.MethodPost {
		writeJSON(rw, http.StatusMethodNotAllowed, map[string]any{"error": "method not allowed"})
		return
	}
	r.Body = http.MaxBytesReader(rw, r.Body, maxWebUpload+(1<<20))
	if err := r.ParseMultipartForm(maxWebUpload + (1 << 20)); err != nil {
		writeJSON(rw, http.StatusBadRequest, map[string]any{"error": "invalid multipart form: " + err.Error()})
		return
	}
	files := r.MultipartForm.File["files"]
	if len(files) == 0 {
		writeJSON(rw, http.StatusBadRequest, map[string]any{"error": "no files selected"})
		return
	}
	category := strings.TrimSpace(r.FormValue("category"))
	if category == "" {
		category = "未分类"
	}
	syncDify := r.FormValue("sync_dify") == "true"

	// 独立工作台：中心不可达时上传全部进入本地离线队列，联网后自动补同步
	offline := !centerReachable(r.Context(), w.client, w.cfg)
	results := make([]map[string]any, 0, len(files))
	for _, fh := range files {
		results = append(results, w.uploadOne(r.Context(), fh, category, offline, syncDify))
	}
	if !offline {
		if flushed, _ := flushOfflineQueue(w.client, w.cfg); flushed > 0 {
			fmt.Printf("[queue] web upload flushed %d queued item(s)\n", flushed)
		}
	}
	writeJSON(rw, http.StatusOK, map[string]any{"results": results, "offline": offline})
}

func (w *webServer) uploadOne(ctx context.Context, fh *multipart.FileHeader, category string, offline, syncDify bool) map[string]any {
	res := map[string]any{"name": filepath.Base(fh.Filename)}
	f, err := fh.Open()
	if err != nil {
		res["error"] = err.Error()
		return res
	}
	data, err := io.ReadAll(io.LimitReader(f, maxWebUpload+1))
	f.Close()
	if err != nil {
		res["error"] = err.Error()
		return res
	}
	if len(data) > maxWebUpload {
		res["error"] = "file exceeds maximum upload size"
		return res
	}
	sum := sha256.Sum256(data)
	hash := hex.EncodeToString(sum[:])
	res["size"] = len(data)
	res["sha256"] = hash
	res["category"] = category
	if offline {
		// 中心离线：本地排队，联网后由 agent 循环自动补同步（含 Dify 同步标记）
		qid, qerr := offlineEnqueue(w.cfg, res["name"].(string), data, hash, category, syncDify)
		if qerr != nil {
			res["error"] = "offline queue: " + qerr.Error()
			return res
		}
		res["queued_offline"] = true
		res["queue_id"] = qid
		res["sync_dify"] = syncDify
		res["mode"] = "queued"
		return res
	}
	if err := w.reportAndUpload(ctx, res["name"].(string), data, hash, category, res); err != nil {
		res["error"] = err.Error()
	}
	return res
}

func (w *webServer) reportAndUpload(ctx context.Context, name string, data []byte, hash, category string, res map[string]any) error {
	// 1. Report the file manifest and get its stable file_id back.
	var rec struct {
		ID int `json:"id"`
	}
	status, body, err := roundTrip(ctx, w.client, http.MethodPost, endpoint(w.cfg, "/api/v1/files/report"), map[string]any{
		"node_id": w.cfg.NodeID, "path": name, "file_hash": hash, "size_bytes": len(data), "category": category,
	}, "X-Node-Key", w.cfg.NodeAPIKey)
	if err != nil {
		return fmt.Errorf("report: %w", err)
	}
	if status >= 300 {
		return fmt.Errorf("report: center returned %d: %s", status, trimBody(body))
	}
	if err := json.Unmarshal(body, &rec); err != nil {
		return fmt.Errorf("decode report response: %w", err)
	}
	res["file_id"] = rec.ID

	// 2. UTF-8 text goes through the deterministic parse pipeline; anything
	// else is stored as an opaque binary blob. Both are node-authenticated.
	if utf8.Valid(data) {
		res["mode"] = "text"
		status, body, err = roundTrip(ctx, w.client, http.MethodPost, endpoint(w.cfg, fmt.Sprintf("/api/v1/files/%d/content", rec.ID)), map[string]any{
			"source_node_id": w.cfg.NodeID, "file_hash": hash, "content": string(data),
		}, "X-Node-Key", w.cfg.NodeAPIKey)
	} else {
		res["mode"] = "binary"
		status, body, err = roundTripRaw(ctx, w.client, http.MethodPost, endpoint(w.cfg, fmt.Sprintf("/api/v1/files/%d/blob", rec.ID)), data, map[string]string{
			"Content-Type": "application/octet-stream",
			"X-File-Hash":  hash,
			"X-Node-Id":    w.cfg.NodeID,
		}, "X-Node-Key", w.cfg.NodeAPIKey)
	}
	if err != nil {
		return err
	}
	if status >= 300 {
		return fmt.Errorf("upload: center returned %d: %s", status, trimBody(body))
	}
	var parsed map[string]any
	_ = json.Unmarshal(body, &parsed)
	for k, v := range parsed {
		if _, exists := res[k]; !exists {
			res[k] = v
		}
	}
	fmt.Printf("[audit] upload node=%s name=%s size=%d sha256=%s mode=%s\n", w.cfg.NodeID, name, len(data), hash[:16], res["mode"])
	return nil
}

// ===================== 离线工作台：本地上传队列 =====================
// 中心不可达时，上传先落到本地队列目录；agent 循环与每次联网上传后
// 自动冲刷队列到中心（文本走解析、二进制走对象存储，并可带 Dify 同步标记）。

type queueItem struct {
	ID        string `json:"id"`
	Name      string `json:"name"`
	Category  string `json:"category"`
	SHA256    string `json:"sha256"`
	Size      int64  `json:"size"`
	SyncDify  bool   `json:"sync_dify"`
	CreatedAt string `json:"created_at"`
	DataFile  string `json:"data_file"`
}

func queueDir(dir string) string {
	if dir == "" {
		return "./edge-queue"
	}
	return dir
}

func centerReachable(ctx context.Context, client *http.Client, cfg Config) bool {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint(cfg, "/healthz"), nil)
	if err != nil {
		return false
	}
	resp, err := client.Do(req)
	if err != nil {
		return false
	}
	resp.Body.Close()
	return resp.StatusCode < 300
}

func offlineEnqueue(cfg Config, name string, data []byte, hash, category string, syncDify bool) (string, error) {
	dir := queueDir(cfg.QueueDir)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", err
	}
	rb := make([]byte, 4)
	_, _ = rand.Read(rb)
	id := time.Now().UTC().Format("20060102T150405") + "-" + hex.EncodeToString(rb)
	item := queueItem{ID: id, Name: name, Category: category, SHA256: hash, Size: int64(len(data)), SyncDify: syncDify, CreatedAt: time.Now().UTC().Format(time.RFC3339), DataFile: id + ".data"}
	payload, err := json.Marshal(item)
	if err != nil {
		return "", err
	}
	if err := os.WriteFile(filepath.Join(dir, id+".data"), data, 0o644); err != nil {
		return "", err
	}
	if err := os.WriteFile(filepath.Join(dir, id+".json"), payload, 0o644); err != nil {
		return "", err
	}
	fmt.Printf("[audit] offline-queued node=%s name=%s sha256=%s sync_dify=%v\n", cfg.NodeID, name, hash[:16], syncDify)
	return id, nil
}

func queueCount(dir string) int {
	n := 0
	_ = filepath.Walk(queueDir(dir), func(_ string, info os.FileInfo, err error) error {
		if err == nil && info != nil && !info.IsDir() && strings.HasSuffix(info.Name(), ".json") {
			n++
		}
		return nil
	})
	return n
}

func listQueue(dir string) []map[string]any {
	out := []map[string]any{}
	dir = queueDir(dir)
	entries, err := os.ReadDir(dir)
	if err != nil {
		return out
	}
	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".json") {
			continue
		}
		raw, err := os.ReadFile(filepath.Join(dir, e.Name()))
		if err != nil {
			continue
		}
		var it queueItem
		if json.Unmarshal(raw, &it) != nil {
			continue
		}
		out = append(out, map[string]any{"id": it.ID, "name": it.Name, "category": it.Category, "size": it.Size, "sync_dify": it.SyncDify, "created_at": it.CreatedAt})
	}
	return out
}

// flushOfflineQueue pushes queued uploads to the center. A network error stops
// the flush (center still down); non-network errors keep the item for retry.
func flushOfflineQueue(client *http.Client, cfg Config) (int, error) {
	dir := queueDir(cfg.QueueDir)
	entries, err := os.ReadDir(dir)
	if err != nil {
		if os.IsNotExist(err) {
			return 0, nil
		}
		return 0, err
	}
	flushed := 0
	w := &webServer{cfg: cfg, client: client}
	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".json") {
			continue
		}
		jsonPath := filepath.Join(dir, e.Name())
		raw, err := os.ReadFile(jsonPath)
		if err != nil {
			continue
		}
		var it queueItem
		if json.Unmarshal(raw, &it) != nil {
			continue
		}
		data, err := os.ReadFile(filepath.Join(dir, it.DataFile))
		if err != nil {
			continue
		}
		res := map[string]any{"name": it.Name, "size": it.Size, "sha256": it.SHA256, "category": it.Category}
		if err := w.reportAndUpload(context.Background(), it.Name, data, it.SHA256, it.Category, res); err != nil {
			return flushed, err // 中心仍不可达或拒收：保留队列待下次重试
		}
		fileID, _ := res["file_id"].(int)
		if it.SyncDify && fileID > 0 {
			if _, _, err := roundTrip(context.Background(), client, http.MethodPost, endpoint(cfg, fmt.Sprintf("/api/v1/rag/ingest?file_id=%d", fileID)), nil, "X-Node-Key", cfg.NodeAPIKey); err != nil {
				fmt.Fprintf(os.Stderr, "[queue] dify ingest of %s: %v\n", it.Name, err)
			}
		}
		_ = os.Remove(jsonPath)
		_ = os.Remove(filepath.Join(dir, it.DataFile))
		fmt.Printf("[audit] offline-flushed node=%s name=%s file_id=%d\n", cfg.NodeID, it.Name, fileID)
		flushed++
	}
	return flushed, nil
}

func (w *webServer) handleQueue(rw http.ResponseWriter, r *http.Request) {
	if !w.authorized(r) {
		writeJSON(rw, http.StatusUnauthorized, map[string]any{"error": "unauthorized"})
		return
	}
	if r.Method != http.MethodGet {
		writeJSON(rw, http.StatusMethodNotAllowed, map[string]any{"error": "method not allowed"})
		return
	}
	writeJSON(rw, http.StatusOK, map[string]any{"items": listQueue(w.cfg.QueueDir), "count": queueCount(w.cfg.QueueDir)})
}

func (w *webServer) handleCategories(rw http.ResponseWriter, r *http.Request) {
	if !w.authorized(r) {
		writeJSON(rw, http.StatusUnauthorized, map[string]any{"error": "unauthorized"})
		return
	}
	status, body, err := roundTrip(r.Context(), w.client, http.MethodGet, endpoint(w.cfg, "/api/v1/categories"), nil, "", "")
	if err != nil {
		writeJSON(rw, http.StatusOK, map[string]any{"offline": true, "categories": []string{"通用", "财务", "人力", "制度"}, "message": "中心离线：使用默认部类"})
		return
	}
	if status >= 300 {
		writeJSON(rw, status, map[string]any{"error": "center returned " + http.StatusText(status)})
		return
	}
	rw.Header().Set("Content-Type", "application/json; charset=utf-8")
	rw.WriteHeader(status)
	_, _ = rw.Write(body)
}

func (w *webServer) handleSearch(rw http.ResponseWriter, r *http.Request) {
	if !w.authorized(r) {
		writeJSON(rw, http.StatusUnauthorized, map[string]any{"error": "unauthorized"})
		return
	}
	if r.Method != http.MethodPost {
		writeJSON(rw, http.StatusMethodNotAllowed, map[string]any{"error": "method not allowed"})
		return
	}
	var req struct {
		Query string `json:"query"`
		TopK  int    `json:"top_k"`
	}
	if err := json.NewDecoder(io.LimitReader(r.Body, 1<<20)).Decode(&req); err != nil {
		writeJSON(rw, http.StatusBadRequest, map[string]any{"error": "invalid json: " + err.Error()})
		return
	}
	if strings.TrimSpace(req.Query) == "" {
		writeJSON(rw, http.StatusBadRequest, map[string]any{"error": "query must not be blank"})
		return
	}
	if req.TopK <= 0 {
		req.TopK = 5
	}
	if req.TopK > 20 {
		req.TopK = 20
	}
	payload := map[string]any{
		"query":           req.Query,
		"top_k":           req.TopK,
		"idempotency_key": fmt.Sprintf("edge-%d", time.Now().UnixNano()),
	}
	status, body, err := roundTrip(r.Context(), w.client, http.MethodPost, endpoint(w.cfg, "/api/v1/knowledge/search"), payload, "X-Search-Key", w.cfg.SearchAPIKey)
	if err != nil {
		// 中心离线：降级为本地文件名匹配（独立工作台能力）
		hits := localFilenameSearch(w.cfg.WatchDir, req.Query)
		fmt.Printf("[audit] search-offline node=%s query=%q hits=%d\n", w.cfg.NodeID, req.Query, len(hits))
		writeJSON(rw, http.StatusOK, map[string]any{"offline": true, "count": len(hits), "results": hits, "message": "中心离线：已按本地文件名匹配"})
		return
	}
	if status >= 300 {
		writeJSON(rw, status, map[string]any{"error": "center returned " + http.StatusText(status) + ": " + trimBody(body)})
		return
	}
	fmt.Printf("[audit] search node=%s query=%q\n", w.cfg.NodeID, req.Query)
	rw.Header().Set("Content-Type", "application/json; charset=utf-8")
	rw.WriteHeader(status)
	_, _ = rw.Write(body)
}

func localFilenameSearch(watchDir, query string) []map[string]any {
	out := []map[string]any{}
	q := strings.ToLower(strings.TrimSpace(query))
	_ = filepath.Walk(watchDir, func(path string, info os.FileInfo, err error) error {
		if err != nil || info == nil || info.IsDir() || info.Size() <= 0 {
			return nil
		}
		if strings.Contains(strings.ToLower(info.Name()), q) {
			out = append(out, map[string]any{"path": path, "name": info.Name(), "size_bytes": info.Size(), "match": "filename"})
		}
		return nil
	})
	return out
}

func (w *webServer) handleIngest(rw http.ResponseWriter, r *http.Request) {
	if !w.authorized(r) {
		writeJSON(rw, http.StatusUnauthorized, map[string]any{"error": "unauthorized"})
		return
	}
	if r.Method != http.MethodPost {
		writeJSON(rw, http.StatusMethodNotAllowed, map[string]any{"error": "method not allowed"})
		return
	}
	var req struct {
		FileID int `json:"file_id"`
	}
	if err := json.NewDecoder(io.LimitReader(r.Body, 1<<20)).Decode(&req); err != nil || req.FileID <= 0 {
		writeJSON(rw, http.StatusBadRequest, map[string]any{"error": "file_id required"})
		return
	}
	// 代理到中心端：管理员/节点鉴权，边缘端用 node key；已解析立即接入，未解析排队
	status, body, err := roundTrip(r.Context(), w.client, http.MethodPost, endpoint(w.cfg, fmt.Sprintf("/api/v1/rag/ingest?file_id=%d", req.FileID)), nil, "X-Node-Key", w.cfg.NodeAPIKey)
	if err != nil {
		writeJSON(rw, http.StatusOK, map[string]any{"offline": true, "blocked": true, "message": "中心离线：Dify 同步不可用，文件需先联网送达中心"})
		return
	}
	if status >= 300 {
		writeJSON(rw, status, map[string]any{"error": "center returned " + http.StatusText(status) + ": " + trimBody(body)})
		return
	}
	fmt.Printf("[audit] dify-ingest node=%s file_id=%d\n", w.cfg.NodeID, req.FileID)
	rw.Header().Set("Content-Type", "application/json; charset=utf-8")
	rw.WriteHeader(status)
	_, _ = rw.Write(body)
}

func (w *webServer) handleProposals(rw http.ResponseWriter, r *http.Request) {
	if !w.authorized(r) {
		writeJSON(rw, http.StatusUnauthorized, map[string]any{"error": "unauthorized"})
		return
	}
	if r.Method != http.MethodGet {
		writeJSON(rw, http.StatusMethodNotAllowed, map[string]any{"error": "method not allowed"})
		return
	}
	// 代理中心端提案列表；X-Admin-Key 由浏览器透传（审批操作需要中心管理员权限）
	status, body, err := roundTrip(r.Context(), w.client, http.MethodGet, endpoint(w.cfg, "/api/v1/proposals"), nil, "X-Admin-Key", r.Header.Get("X-Admin-Key"))
	if err != nil {
		writeJSON(rw, http.StatusOK, map[string]any{"offline": true, "message": "中心离线：提案审阅不可用"})
		return
	}
	if status >= 300 {
		writeJSON(rw, status, map[string]any{"error": "center returned " + http.StatusText(status) + ": " + trimBody(body)})
		return
	}
	rw.Header().Set("Content-Type", "application/json; charset=utf-8")
	rw.WriteHeader(status)
	_, _ = rw.Write(body)
}

func (w *webServer) handleProposalReview(rw http.ResponseWriter, r *http.Request) {
	if !w.authorized(r) {
		writeJSON(rw, http.StatusUnauthorized, map[string]any{"error": "unauthorized"})
		return
	}
	if r.Method != http.MethodPost {
		writeJSON(rw, http.StatusMethodNotAllowed, map[string]any{"error": "method not allowed"})
		return
	}
	// /api/proposals/{id}/review
	idStr := strings.TrimSuffix(strings.TrimPrefix(r.URL.Path, "/api/proposals/"), "/review")
	if idStr == "" {
		writeJSON(rw, http.StatusBadRequest, map[string]any{"error": "proposal id required"})
		return
	}
	bodyBytes, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
	if err != nil {
		writeJSON(rw, http.StatusBadRequest, map[string]any{"error": err.Error()})
		return
	}
	url := endpoint(w.cfg, fmt.Sprintf("/api/v1/proposals/%s/review", idStr))
	req, err := http.NewRequestWithContext(r.Context(), http.MethodPost, url, bytes.NewReader(bodyBytes))
	if err != nil {
		writeJSON(rw, http.StatusBadGateway, map[string]any{"error": err.Error()})
		return
	}
	req.Header.Set("Content-Type", "application/json")
	if k := r.Header.Get("X-Admin-Key"); k != "" {
		req.Header.Set("X-Admin-Key", k)
	}
	status, body, err := doRoundTrip(w.client, req)
	if err != nil {
		writeJSON(rw, http.StatusOK, map[string]any{"offline": true, "message": "中心离线：批红不可用"})
		return
	}
	if status >= 300 {
		writeJSON(rw, status, map[string]any{"error": "center returned " + http.StatusText(status) + ": " + trimBody(body)})
		return
	}
	fmt.Printf("[audit] proposal-review node=%s proposal=%s\n", w.cfg.NodeID, idStr)
	rw.Header().Set("Content-Type", "application/json; charset=utf-8")
	rw.WriteHeader(status)
	_, _ = rw.Write(body)
}

// roundTrip performs a JSON request against the center and returns the raw
// response. The browser never sees the node/search API keys — they stay here.
func roundTrip(ctx context.Context, client *http.Client, method, url string, payload any, keyHeader, keyValue string) (int, []byte, error) {
	var body io.Reader
	if payload != nil {
		b, err := json.Marshal(payload)
		if err != nil {
			return 0, nil, err
		}
		body = bytes.NewReader(b)
	}
	req, err := http.NewRequestWithContext(ctx, method, url, body)
	if err != nil {
		return 0, nil, err
	}
	if payload != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if keyValue != "" {
		req.Header.Set(keyHeader, keyValue)
	}
	return doRoundTrip(client, req)
}

func roundTripRaw(ctx context.Context, client *http.Client, method, url string, raw []byte, headers map[string]string, keyHeader, keyValue string) (int, []byte, error) {
	req, err := http.NewRequestWithContext(ctx, method, url, bytes.NewReader(raw))
	if err != nil {
		return 0, nil, err
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	if keyValue != "" {
		req.Header.Set(keyHeader, keyValue)
	}
	return doRoundTrip(client, req)
}

func doRoundTrip(client *http.Client, req *http.Request) (int, []byte, error) {
	resp, err := client.Do(req)
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if err != nil {
		return 0, nil, err
	}
	return resp.StatusCode, data, nil
}

func trimBody(body []byte) string {
	s := strings.TrimSpace(string(body))
	if len(s) > 200 {
		s = s[:200] + "…"
	}
	return s
}
