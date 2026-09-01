// KMill machine agent — a tiny, view-only HTTP server that runs on each
// milling-machine PC (RemiCORE / imes-icore). The KMill CRM PULLS from it
// (the CRM binds to 127.0.0.1 only, so it reaches OUT to the machine, exactly
// like the existing VNC path) and OCRs the returned frame.
//
// Design goals: single static .exe, no runtime, Windows 7/8/10/11, near-zero
// idle load (it only does work when the CRM polls), and future-proof — new
// capture needs (a display index, later a crop rect / structured field) are
// added as request parameters or config keys without redeploying to machines.
//
// Endpoints (all except /healthz require the shared token):
//   GET /healthz                 -> "ok"                (liveness, no auth)
//   GET /status                  -> JSON {name,hostname,displays,version,time}
//   GET /capture[?display=N]     -> image/png            (a full display frame)
//
// Auth: header `X-Agent-Token: <token>` or `?token=<token>` (constant-time).
package main

import (
	"bytes"
	"crypto/subtle"
	"encoding/json"
	"flag"
	"fmt"
	"image/png"
	"io"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"time"

	"github.com/kbinani/screenshot"
)

// Version is stamped at build time via -ldflags "-X main.Version=...".
var Version = "dev"

// Config is read from agent.json next to the exe (override with -config).
type Config struct {
	Bind    string `json:"bind"`    // listen address, e.g. "0.0.0.0:8765"
	Token   string `json:"token"`   // shared secret the CRM must present
	Display int    `json:"display"` // default display index (0 = primary)
	Name    string `json:"name"`    // optional human label for this machine
}

func loadConfig(path string) (Config, error) {
	cfg := Config{Bind: "0.0.0.0:8765", Display: 0}
	b, err := os.ReadFile(path)
	if err != nil {
		return cfg, err
	}
	if err := json.Unmarshal(b, &cfg); err != nil {
		return cfg, fmt.Errorf("parse %s: %w", path, err)
	}
	if cfg.Bind == "" {
		cfg.Bind = "0.0.0.0:8765"
	}
	return cfg, nil
}

func main() {
	exePath, _ := os.Executable()
	exeDir := filepath.Dir(exePath)

	cfgPath := flag.String("config", filepath.Join(exeDir, "agent.json"), "path to agent.json")
	flag.Parse()

	// Log to a file next to the exe (the release build hides the console, so
	// stderr would go nowhere). Best-effort; falls back to stderr.
	if f, err := os.OpenFile(filepath.Join(exeDir, "kmill-agent.log"),
		os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0644); err == nil {
		log.SetOutput(io.MultiWriter(os.Stderr, f))
	}

	cfg, err := loadConfig(*cfgPath)
	if err != nil {
		log.Fatalf("config error: %v", err)
	}
	if cfg.Token == "" {
		log.Fatalf("config error: 'token' must be set in %s", *cfgPath)
	}

	authed := func(next http.HandlerFunc) http.HandlerFunc {
		return func(w http.ResponseWriter, r *http.Request) {
			t := r.Header.Get("X-Agent-Token")
			if t == "" {
				t = r.URL.Query().Get("token")
			}
			if subtle.ConstantTimeCompare([]byte(t), []byte(cfg.Token)) != 1 {
				http.Error(w, "forbidden", http.StatusForbidden)
				return
			}
			next(w, r)
		}
	}

	mux := http.NewServeMux()

	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.WriteString(w, "ok")
	})

	mux.HandleFunc("/status", authed(func(w http.ResponseWriter, r *http.Request) {
		host, _ := os.Hostname()
		resp := map[string]interface{}{
			"name":     cfg.Name,
			"hostname": host,
			"displays": screenshot.NumActiveDisplays(),
			"version":  Version,
			"time":     time.Now().Format(time.RFC3339),
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(resp)
	}))

	mux.HandleFunc("/capture", authed(func(w http.ResponseWriter, r *http.Request) {
		n := screenshot.NumActiveDisplays()
		if n <= 0 {
			http.Error(w, "no active displays", http.StatusServiceUnavailable)
			return
		}
		disp := cfg.Display
		if q := r.URL.Query().Get("display"); q != "" {
			if v, err := fmt.Sscanf(q, "%d", &disp); err != nil || v != 1 {
				disp = cfg.Display
			}
		}
		if disp < 0 || disp >= n {
			disp = 0
		}
		img, err := screenshot.CaptureRect(screenshot.GetDisplayBounds(disp))
		if err != nil {
			log.Printf("capture failed: %v", err)
			http.Error(w, "capture failed", http.StatusInternalServerError)
			return
		}
		var buf bytes.Buffer
		if err := png.Encode(&buf, img); err != nil {
			http.Error(w, "encode failed", http.StatusInternalServerError)
			return
		}
		w.Header().Set("Content-Type", "image/png")
		w.Header().Set("Cache-Control", "no-store")
		_, _ = w.Write(buf.Bytes())
	}))

	log.Printf("kmill-agent %s listening on %s (default display %d)", Version, cfg.Bind, cfg.Display)
	srv := &http.Server{
		Addr:         cfg.Bind,
		Handler:      mux,
		ReadTimeout:  10 * time.Second,
		WriteTimeout: 30 * time.Second,
		IdleTimeout:  60 * time.Second,
	}
	log.Fatal(srv.ListenAndServe())
}
