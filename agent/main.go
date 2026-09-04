// KMill machine agent — a tiny, view-only HTTP server that runs on each
// milling-machine PC (RemiCORE / imes-icore). The KMill CRM PULLS from it
// (the CRM binds to 127.0.0.1 only, so it reaches OUT to the machine, exactly
// like the existing VNC path) and OCRs the returned frame.
//
// Two run modes:
//
//   kmill-agent.exe -serve   — background capture server (the scheduled task
//                              runs this). Requires a token in agent.json.
//   kmill-agent.exe -setup   — opens a local settings page in the browser:
//                              type token / name / display, read this PC's IP
//                              and the ready-to-paste CRM address, see a live
//                              frame, Save (registers autostart + restarts).
//   kmill-agent.exe          — no args: same as -setup (so a double-click on
//                              the exe opens the settings menu).
//
// Design goals: single static .exe, no runtime, Windows 7/8/10/11, near-zero
// idle load, nothing "calls home" on its own.
//
// Capture endpoints (all except /healthz require the shared token):
//   GET /healthz                 -> "ok"                (liveness, no auth)
//   GET /status                  -> JSON {name,hostname,displays,version,time}
//   GET /capture[?display=N]     -> image/png            (a full display frame)
//
// Auth: header `X-Agent-Token: <token>` or `?token=<token>` (constant-time).
package main

import (
	"bytes"
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"image/png"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync/atomic"
	"time"
	stdutf16 "unicode/utf16"

	"github.com/kbinani/screenshot"
)

// Version is stamped at build time via -ldflags "-X main.Version=...".
var Version = "dev"

// Пауза спостереження — перемикається з трею. Поки ввімкнена, агент НЕ віддає
// кадр і не називає програму: людина за верстатом має мати змогу тимчасово
// закрити свій екран, не викликаючи адміна й не вбиваючи процес. CRM при цьому
// чесно показує «немає зв'язку», а не застиглий старий кадр.
var paused atomic.Bool

func isPaused() bool { return paused.Load() }

func togglePaused() {
	paused.Store(!paused.Load())
	log.Printf("спостереження: %s", map[bool]string{true: "призупинено", false: "активне"}[paused.Load()])
	updateTrayIcon()
}

// taskName is the Task Scheduler entry that starts the agent at logon.
const taskName = "KMillAgent"

// setupAddr is the loopback-only address the settings page listens on. It is
// deliberately different from the capture port so the settings page can run
// while the background task already holds the capture port.
const setupAddr = "127.0.0.1:8766"

// Config is read from agent.json next to the exe (override with -config).
type Config struct {
	Bind    string `json:"bind"`    // listen address, e.g. "0.0.0.0:8765"
	Token   string `json:"token"`   // shared secret the CRM must present
	Display int    `json:"display"` // default display index (0 = primary)
	Name    string `json:"name"`    // optional human label for this machine
}

func defaultConfig() Config { return Config{Bind: "0.0.0.0:8765", Display: 0} }

func loadConfig(path string) (Config, error) {
	cfg := defaultConfig()
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

func saveConfig(path string, cfg Config) error {
	b, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		return err
	}
	// Write atomically: temp file + rename, so a crash never leaves a half file.
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, b, 0644); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// port returns the numeric port from a bind string ("0.0.0.0:8765" -> "8765").
func portOf(bind string) string {
	if i := strings.LastIndex(bind, ":"); i >= 0 {
		return bind[i+1:]
	}
	return "8765"
}

// randomToken returns a 32-char hex string (16 random bytes).
func randomToken() string {
	b := make([]byte, 16)
	if _, err := rand.Read(b); err != nil {
		return "changeme-" + strconv.FormatInt(time.Now().UnixNano(), 16)
	}
	return hex.EncodeToString(b)
}

// localIPv4s returns this PC's non-loopback IPv4 addresses (for the CRM URL).
func localIPv4s() []string {
	var out []string
	ifaces, err := net.Interfaces()
	if err != nil {
		return out
	}
	for _, ifc := range ifaces {
		if ifc.Flags&net.FlagUp == 0 || ifc.Flags&net.FlagLoopback != 0 {
			continue
		}
		addrs, _ := ifc.Addrs()
		for _, a := range addrs {
			var ip net.IP
			switch v := a.(type) {
			case *net.IPNet:
				ip = v.IP
			case *net.IPAddr:
				ip = v.IP
			}
			if ip == nil || ip.IsLoopback() {
				continue
			}
			if v4 := ip.To4(); v4 != nil {
				out = append(out, v4.String())
			}
		}
	}
	return out
}

func exeDir() string {
	exePath, _ := os.Executable()
	return filepath.Dir(exePath)
}

func exePath() string {
	p, _ := os.Executable()
	return p
}

func main() {
	cfgPath := flag.String("config", filepath.Join(exeDir(), "agent.json"), "path to agent.json")
	serve := flag.Bool("serve", false, "run the background capture server (used by the scheduled task)")
	// -setup is accepted for clarity but no-args already opens the settings page,
	// so the flag is registered only to be a valid argument.
	_ = flag.Bool("setup", false, "open the settings page in the browser")
	install := flag.Bool("install", false, "one elevated pass: config+token, autostart, firewall, start (installer runs this)")
	elevated := flag.Bool("elevated", false, "internal: set after relaunching with admin rights")
	flag.Parse()

	// Log to a file next to the exe (the release build hides the console).
	if f, err := os.OpenFile(filepath.Join(exeDir(), "kmill-agent.log"),
		os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0644); err == nil {
		log.SetOutput(io.MultiWriter(os.Stderr, f))
	}

	if *install {
		runInstall(*cfgPath)
		return
	}
	if *serve {
		runServe(*cfgPath)
		return
	}
	// No -serve → settings menu (default, so a double-click opens it too).
	runSetup(*cfgPath, *elevated)
}

// ---------------------------------------------------------------------------
// Background capture server (the scheduled task runs `-serve`).
// ---------------------------------------------------------------------------

func runServe(cfgPath string) {
	cfg, err := loadConfig(cfgPath)
	if err != nil {
		log.Fatalf("config error: %v (run `kmill-agent.exe -setup` first)", err)
	}
	if cfg.Token == "" {
		log.Fatalf("config error: 'token' not set in %s (run -setup)", cfgPath)
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
		writeJSON(w, map[string]interface{}{
			"name":     cfg.Name,
			"hostname": host,
			"displays": screenshot.NumActiveDisplays(),
			"version":  Version,
			"time":     time.Now().Format(time.RFC3339),
		})
	}))
	mux.HandleFunc("/capture", authed(func(w http.ResponseWriter, r *http.Request) {
		if isPaused() {
			http.Error(w, "спостереження призупинено на верстаті", http.StatusServiceUnavailable)
			return
		}
		captureToResponse(w, r, cfg.Display)
	}))
	// Заголовки вікон: у заголовку RemiCORE лежить повне ім'я .iso-програми,
	// а в ньому дата+час = Sum3D ID. Читаємо ТЕКСТОМ, не OCR — точно й дешево.
	mux.HandleFunc("/titles", authed(func(w http.ResponseWriter, r *http.Request) {
		if isPaused() {
			http.Error(w, "спостереження призупинено на верстаті", http.StatusServiceUnavailable)
			return
		}
		writeJSON(w, map[string]interface{}{"titles": visibleWindowTitles()})
	}))

	// Also serve the settings menu on loopback 8766 so 127.0.0.1:8766 ALWAYS
	// works while the agent runs — no separate shortcut needed, and Save runs
	// inside this (elevated) process so it can register autostart/firewall.
	go func() {
		if err := http.ListenAndServe(setupAddr, newSetupMux(cfgPath, false)); err != nil {
			log.Printf("settings menu not served on %s: %v", setupAddr, err)
		}
	}()

	log.Printf("kmill-agent %s serving on %s (display %d)", Version, cfg.Bind, cfg.Display)
	srv := &http.Server{
		Addr:         cfg.Bind,
		Handler:      mux,
		ReadTimeout:  10 * time.Second,
		WriteTimeout: 30 * time.Second,
		IdleTimeout:  60 * time.Second,
	}
	// HTTP — у горутині, трей тримає ГОЛОВНИЙ потік: цикл повідомлень Windows
	// мусить жити саме там. «Вийти» в треї зупиняє процес цілком.
	//
	// НЕ Fatalf на помилці Serve. На верстатному ПК мережа зникає й вертається
	// (кабель, світч, засинання NIC), і на Windows слухаючий сокет від цього
	// ламається — ListenAndServe повертає помилку. Раніше тут процес умирав, а
	// автозапуск (schtasks /sc onlogon) підіймав його лише при НАСТУПНОМУ вході,
	// тож верстат лишався «offline» до логіну. Тепер сервер сам перепідключається:
	// коли мережа вертається, re-listen вдається, і CRM знову його бачить —
	// без жодного ручного втручання.
	go func() {
		for {
			err := srv.ListenAndServe()
			if err == http.ErrServerClosed {
				return // штатна зупинка (не трапляється тут, але коректно)
			}
			log.Printf("сервер зупинився (%v) — перезапуск слухача за 5 с", err)
			time.Sleep(5 * time.Second)
			// Той самий srv після Serve вважається завершеним; новий екземпляр
			// на ту саму адресу переслуховує порт, щойно мережа дозволить.
			srv = &http.Server{
				Addr:         cfg.Bind,
				Handler:      mux,
				ReadTimeout:  10 * time.Second,
				WriteTimeout: 30 * time.Second,
				IdleTimeout:  60 * time.Second,
			}
		}
	}()
	runTray(func() { log.Printf("вихід із трею") })
	// Трей недоступний (немає сесії робочого столу) — не залишати процес без
	// роботи: тримаємо HTTP далі, як і до появи іконки.
	select {}
}

// captureToResponse grabs the frame for the requested (or default) display and
// writes it as PNG.
func captureToResponse(w http.ResponseWriter, r *http.Request, def int) {
	n := screenshot.NumActiveDisplays()
	if n <= 0 {
		http.Error(w, "no active displays", http.StatusServiceUnavailable)
		return
	}
	disp := def
	if q := r.URL.Query().Get("display"); q != "" {
		if v, err := strconv.Atoi(q); err == nil {
			disp = v
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
}

func writeJSON(w http.ResponseWriter, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(v)
}

// ---------------------------------------------------------------------------
// Settings menu (`-setup`) — a small local web page.
// ---------------------------------------------------------------------------

// newSetupMux builds the settings-menu handlers. `allowQuit` is true only for
// the standalone -setup process (its /quit may exit); when the background
// -serve hosts the menu, quit must NOT kill the agent, so it is a no-op there.
func newSetupMux(cfgPath string, allowQuit bool) *http.ServeMux {
	mux := http.NewServeMux()

	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = io.WriteString(w, setupHTML)
	})

	mux.HandleFunc("/info", func(w http.ResponseWriter, r *http.Request) {
		cur, _ := loadConfig(cfgPath)
		if cur.Token == "" {
			cur.Token = randomToken()
		}
		host, _ := os.Hostname()
		installed, running := taskState()
		writeJSON(w, map[string]interface{}{
			"token":         cur.Token,
			"name":          cur.Name,
			"display":       cur.Display,
			"port":          portOf(cur.Bind),
			"hostname":      host,
			"ips":           localIPv4s(),
			"displays":      screenshot.NumActiveDisplays(),
			"version":       Version,
			"admin":         isAdmin(),
			"taskInstalled": installed,
			"taskRunning":   running,
		})
	})

	mux.HandleFunc("/preview", func(w http.ResponseWriter, r *http.Request) {
		cur, _ := loadConfig(cfgPath)
		captureToResponse(w, r, cur.Display)
	})

	mux.HandleFunc("/save", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "POST only", http.StatusMethodNotAllowed)
			return
		}
		var in struct {
			Token   string `json:"token"`
			Name    string `json:"name"`
			Display int    `json:"display"`
			Port    string `json:"port"`
		}
		if err := json.NewDecoder(r.Body).Decode(&in); err != nil {
			writeJSON(w, map[string]interface{}{"ok": false, "error": "невірні дані форми"})
			return
		}
		in.Token = strings.TrimSpace(in.Token)
		if len(in.Token) < 8 {
			writeJSON(w, map[string]interface{}{"ok": false, "error": "токен закороткий (мін. 8 символів)"})
			return
		}
		port := strings.TrimSpace(in.Port)
		if port == "" {
			port = "8765"
		}
		if _, err := strconv.Atoi(port); err != nil {
			writeJSON(w, map[string]interface{}{"ok": false, "error": "порт має бути числом"})
			return
		}
		newCfg := Config{
			Bind:    "0.0.0.0:" + port,
			Token:   in.Token,
			Display: in.Display,
			Name:    strings.TrimSpace(in.Name),
		}
		if err := saveConfig(cfgPath, newCfg); err != nil {
			writeJSON(w, map[string]interface{}{"ok": false, "error": "не вдалось зберегти agent.json: " + err.Error()})
			return
		}
		var warns []string
		if err := registerTask(); err != nil {
			warns = append(warns, "автозапуск не зареєстровано: "+err.Error())
		}
		if err := openFirewall(port); err != nil {
			warns = append(warns, "правило брандмауера не додано: "+err.Error())
		}
		if err := restartTask(); err != nil {
			warns = append(warns, "агент не перезапущено: "+err.Error())
		}
		writeJSON(w, map[string]interface{}{"ok": true, "warnings": warns})
	})

	mux.HandleFunc("/quit", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, map[string]interface{}{"ok": true})
		if allowQuit {
			go func() { time.Sleep(300 * time.Millisecond); os.Exit(0) }()
		}
	})

	return mux
}

// runSetup opens the settings menu in a browser. Since the background -serve
// now also serves the menu on 8766, this usually finds the port busy and just
// points the browser at the already-running page — no elevation dance needed.
func runSetup(cfgPath string, elevated bool) {
	url := "http://" + setupAddr + "/"
	ln, err := net.Listen("tcp", setupAddr)
	if err != nil {
		// The background agent already serves the menu — just open it.
		log.Printf("menu already served (%v); opening browser", err)
		openBrowser(url)
		return
	}
	// No background agent holding the port: this standalone process serves the
	// menu itself. Elevate so Save can register autostart / firewall.
	if !elevated && !isAdmin() {
		if err := relaunchElevated("-setup", "-elevated"); err == nil {
			_ = ln.Close()
			return // the elevated copy takes over
		}
		log.Printf("could not elevate; Save may fail to register autostart")
	}
	log.Printf("kmill-agent %s settings on %s", Version, url)
	openBrowser(url)
	log.Fatal(http.Serve(ln, newSetupMux(cfgPath, true)))
}

// runInstall does the whole first-time setup in ONE elevated pass (the
// installer runs it): create config with a token if missing, register the
// autostart task, open the firewall, and start the agent now. No manual
// «Зберегти», no per-click UAC. Writes the token + CRM address to a file the
// installer shows, so the operator only has to paste them into KMill.
func runInstall(cfgPath string) {
	cfg, err := loadConfig(cfgPath)
	if err != nil {
		cfg = defaultConfig()
	}
	if cfg.Token == "" {
		cfg.Token = randomToken()
	}
	if strings.TrimSpace(cfg.Name) == "" {
		if host, e := os.Hostname(); e == nil {
			cfg.Name = host
		}
	}
	if cfg.Bind == "" {
		cfg.Bind = "0.0.0.0:8765"
	}
	if err := saveConfig(cfgPath, cfg); err != nil {
		log.Printf("install: could not write config: %v", err)
	}
	if err := registerTask(); err != nil {
		log.Printf("install: register task failed: %v", err)
	}
	// Другий, незалежний автозапуск — ярлик в автозавантаженні. Якщо задача
	// планувальника не спрацює (а таке буває), агент однаково підніметься при
	// вході користувача. Помилка тут не фатальна: задача лишається основною.
	if err := addStartupShortcut(); err != nil {
		log.Printf("install: startup shortcut failed: %v", err)
	}
	if err := openFirewall(portOf(cfg.Bind)); err != nil {
		log.Printf("install: firewall rule failed: %v", err)
	}
	if err := restartTask(); err != nil {
		log.Printf("install: start agent failed: %v", err)
	}
	// A copy-paste cheat-sheet for the operator, shown by the installer.
	var b strings.Builder
	b.WriteString("KMill Agent — дані для CRM (Налаштування → Верстати)\r\n\r\n")
	b.WriteString("Токен агента:\r\n  " + cfg.Token + "\r\n\r\n")
	b.WriteString("Порт: " + portOf(cfg.Bind) + "\r\n\r\nАдреса (IP цього ПК):\r\n")
	for _, ip := range localIPv4s() {
		b.WriteString("  " + ip + ":" + portOf(cfg.Bind) + "\r\n")
	}
	b.WriteString("\r\nМеню налаштувань (будь-коли): http://127.0.0.1:8766\r\n")
	_ = os.WriteFile(filepath.Join(exeDir(), "crm-setup.txt"), []byte(b.String()), 0644)
	log.Printf("install done: token set, task+firewall+serve up")
}

// ---------------------------------------------------------------------------
// Windows plumbing (schtasks / netsh / browser). No-ops off Windows so the
// package still builds on the Linux CI host during `go mod tidy`.
// ---------------------------------------------------------------------------

// registerTask реєструє автозапуск при вході через XML, а не рядок команди.
// Причина: рядковий `schtasks /sc onlogon /rl highest` не вміє ні
// restart-on-failure, ні StartWhenAvailable, а елевейтед-onlogon на Win10
// крихкий — після ребуту агент часто не піднімався (бойовий випадок 04.09.26,
// довелось стартувати руками). XML дає:
//   • onlogon БУДЬ-ЯКОГО користувача (без прив'язки до імені — не розсинхрону);
//   • LeastPrivilege: серверу адмін не потрібен (порт 8765 і знімок екрана
//     працюють без прав), а неелевейтед-задача при вході надійніша;
//   • RestartOnFailure — упав, перезапуститься сам за хвилину;
//   • StartWhenAvailable — надолужить, якщо момент входу пропущено;
//   • ExecutionTimeLimit PT0S — сервер живе безкінечно, планувальник його не
//     вбиває за таймером.
func registerTask() error {
	xml := taskXML(exePath())
	tmp := filepath.Join(os.TempDir(), "kmill_agent_task.xml")
	// Task Scheduler чекає UTF-16; кладемо BOM + перекодовуємо.
	if err := os.WriteFile(tmp, utf16WithBOM(xml), 0644); err != nil {
		return fmt.Errorf("write task xml: %w", err)
	}
	defer os.Remove(tmp)
	return runCmd("schtasks", "/create", "/tn", taskName, "/xml", tmp, "/f")
}

func taskXML(exe string) string {
	// Без <UserId> у тригері: задача спрацьовує на вхід будь-якого користувача,
	// тож не ламається, якщо машина логіниться під іншим акаунтом.
	return `<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled></LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>` + xmlEscape(exe) + `</Command>
      <Arguments>-serve</Arguments>
      <WorkingDirectory>` + xmlEscape(filepath.Dir(exe)) + `</WorkingDirectory>
    </Exec>
  </Actions>
</Task>`
}

func xmlEscape(s string) string {
	r := strings.NewReplacer("&", "&amp;", "<", "&lt;", ">", "&gt;", `"`, "&quot;")
	return r.Replace(s)
}

// utf16WithBOM кодує рядок у little-endian UTF-16 з BOM — саме цього чекає
// schtasks /xml (інакше «invalid XML»).
func utf16WithBOM(s string) []byte {
	out := []byte{0xFF, 0xFE} // BOM LE
	for _, r := range stdutf16.Encode([]rune(s)) {
		out = append(out, byte(r), byte(r>>8))
	}
	return out
}

// addStartupShortcut кладе ярлик у теку автозавантаження користувача —
// ДРУГИЙ, незалежний шлях підняти агента при вході. Якщо задача планувальника
// з якоїсь причини не спрацює, ярлик усе одно стартує сервер у сесії
// користувача (де тільки й можливий знімок екрана). Робиться через WScript.Shell
// (створює справжній .lnk), вікно приховане — на верстаті нічого не блимає.
func addStartupShortcut() error {
	lnk := `$s=(New-Object -ComObject WScript.Shell);` +
		`$k=$s.CreateShortcut([System.IO.Path]::Combine($s.SpecialFolders('Startup'),'KMillAgent.lnk'));` +
		`$k.TargetPath=` + psQuote(exePath()) + `;` +
		`$k.Arguments='-serve';` +
		`$k.WorkingDirectory=` + psQuote(exeDir()) + `;` +
		`$k.WindowStyle=7;$k.Save()`
	return runCmd("powershell", "-NoProfile", "-NonInteractive", "-Command", lnk)
}

func psQuote(s string) string {
	// Одинарні лапки PowerShell: подвоїти внутрішні одинарні.
	return "'" + strings.ReplaceAll(s, "'", "''") + "'"
}

func restartTask() error {
	_ = runCmd("schtasks", "/end", "/tn", taskName) // ignore "not running"
	return runCmd("schtasks", "/run", "/tn", taskName)
}

// taskState reports whether the scheduled task exists and is currently running.
func taskState() (installed, running bool) {
	out, err := exec.Command("schtasks", "/query", "/tn", taskName, "/fo", "list", "/v").CombinedOutput()
	if err != nil {
		return false, false
	}
	installed = true
	s := strings.ToLower(string(out))
	running = strings.Contains(s, "running") || strings.Contains(s, "виконується")
	return
}

func openFirewall(port string) error {
	_ = runCmd("netsh", "advfirewall", "firewall", "delete", "rule", "name="+taskName)
	// profile=any явно: на Win10 мережа часто в профілі «Загальна» (Public), і
	// без цього правило могло не діяти саме там, де верстат (бойовий випадок
	// 04.09.26 — порт довелось відкривати руками).
	return runCmd("netsh", "advfirewall", "firewall", "add", "rule",
		"name="+taskName, "dir=in", "action=allow", "protocol=TCP",
		"localport="+port, "profile=any")
}

func runCmd(name string, args ...string) error {
	out, err := exec.Command(name, args...).CombinedOutput()
	if err != nil {
		return fmt.Errorf("%s: %v: %s", name, err, strings.TrimSpace(string(out)))
	}
	return nil
}

func openBrowser(url string) {
	// `cmd /c start "" <url>` opens the default browser without a console.
	_ = exec.Command("cmd", "/c", "start", "", url).Start()
}
