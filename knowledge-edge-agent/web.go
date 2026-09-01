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

	results := make([]map[string]any, 0, len(files))
	for _, fh := range files {
		results = append(results, w.uploadOne(r.Context(), fh, category))
	}
	writeJSON(rw, http.StatusOK, map[string]any{"results": results})
}

func (w *webServer) uploadOne(ctx context.Context, fh *multipart.FileHeader, category string) map[string]any {
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

func (w *webServer) handleCategories(rw http.ResponseWriter, r *http.Request) {
	if !w.authorized(r) {
		writeJSON(rw, http.StatusUnauthorized, map[string]any{"error": "unauthorized"})
		return
	}
	status, body, err := roundTrip(r.Context(), w.client, http.MethodGet, endpoint(w.cfg, "/api/v1/categories"), nil, "", "")
	if err != nil {
		writeJSON(rw, http.StatusBadGateway, map[string]any{"error": "categories: " + err.Error()})
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
		writeJSON(rw, http.StatusBadGateway, map[string]any{"error": "search: " + err.Error()})
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
		writeJSON(rw, http.StatusBadGateway, map[string]any{"error": "ingest: " + err.Error()})
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
		writeJSON(rw, http.StatusBadGateway, map[string]any{"error": "proposals: " + err.Error()})
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
		writeJSON(rw, http.StatusBadGateway, map[string]any{"error": "review: " + err.Error()})
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
