package main

// Самооновлення агента — без прав адміністратора й без походу до верстата.
//
// Встановлений exe (Program Files) лишається таким, яким його поставив
// інсталятор, і стає ЗАПУСКАЧЕМ: на старті шукає у власній теці користувача
// новішу версію з дійсним підписом (internal/upd) і запускає її дочірнім
// процесом, а сам стежить. Нова версія не відповіла або падає — запускач
// позначає її несправною і працює сам, вбудованою версією. Тобто найгірший
// наслідок невдалого оновлення — агент лишається на старій версії.
//
// Чому тека користувача (%LOCALAPPDATA%), а не Program Files і не ProgramData:
//   - у Program Files агент писати не може й не повинен (див. logDirs);
//   - у ProgramData\KMill Agent пишуть усі користувачі ПК, і чужий обліковий
//     запис міг би підкласти туди файл; своя тека користувача закрита від інших.
//
// Чого запускач НЕ робить ніколи:
//   - не запускає файл без підпису, зі зміненим вмістом чи старіший за себе;
//   - не запускає оновлення, коли сам працює з підвищеними правами (після
//     інсталятора, до наступного входу): підміна файла в ту мить дала б
//     звичайній програмі права адміністратора. Тоді оновлення чекає входу;
//   - режими з правами адміністратора (-install, -setup) оновлень не бачать
//     узагалі — вони завжди виконуються встановленим exe.

import (
	"encoding/base64"
	"encoding/hex"
	"errors"
	"log"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"sync/atomic"
	"time"

	"kmill-agent/internal/upd"
)

// BuildStamp — час збірки (секунди Unix), штампує CI через -ldflags. Ним
// версії порівнюються. «0» = локальна збірка: будь-яке підписане оновлення
// для неї новіше.
var BuildStamp = "0"

const (
	// exitRestartForUpdate — оновлена версія просить запускач перечитати теку
	// оновлень (прийшла ще новіша). Будь-який інший ненульовий код — падіння.
	exitRestartForUpdate = 75

	// Скільки чекати, поки нова версія почне відповідати. Агент піднімається
	// за секунду; 45 с — це запас на повільний диск ПК верстата, не норма.
	payloadHealthWait = 45 * time.Second
	payloadHealthStep = time.Second

	updateReadDeadline = 3 * time.Minute

	envBuiltinVersion = "KMILL_AGENT_BUILTIN_VERSION"
	envUpdateNote     = "KMILL_AGENT_UPDATE_NOTE"
)

// launcherPath — шлях до встановленого exe, коли цей процес є оновленою
// версією (прапорець -launcher). Порожньо = ми і є встановлений exe.
var launcherPath string

// updateNote — чому найновіше оновлення НЕ працює (порожньо = все гаразд).
// Єдине, що про невдале оновлення побачить людина в CRM.
var updateNote atomic.Value

// liveServer — поточний HTTP-сервер знімків, щоб звільнити порт перед
// перезапуском (див. restartForUpdate).
var liveServer atomic.Value

func buildStamp() int64 {
	n, err := strconv.ParseInt(BuildStamp, 10, 64)
	if err != nil || n < 0 {
		return 0
	}
	return n
}

func isPayload() bool { return launcherPath != "" }

func setUpdateNote(s string) { updateNote.Store(s) }

func getUpdateNote() string {
	if v, ok := updateNote.Load().(string); ok {
		return v
	}
	return ""
}

// builtinVersion — версія встановленого exe. Оновлена версія дізнається її
// від запускача: сама вона знає лише власну.
func builtinVersion() string {
	if isPayload() {
		return os.Getenv(envBuiltinVersion)
	}
	return Version
}

// updateRoot — тека оновлень цього користувача. Порожньо = немає куди класти
// (тоді самооновлення просто вимкнене).
func updateRoot() string {
	d := os.Getenv("LOCALAPPDATA")
	if d == "" {
		return ""
	}
	return filepath.Join(d, "KMill Agent", "update")
}

// useLauncherPath вмикає режим «я — оновлена версія». Шлях мусить бути
// справжнім файлом: з нього беруться конфіг, автозапуск і все, що раніше
// бралося з os.Executable().
func useLauncherPath(p string) {
	if p == "" {
		return
	}
	abs, err := filepath.Abs(p)
	if err != nil {
		return
	}
	if st, err := os.Stat(abs); err != nil || st.IsDir() {
		log.Printf("оновлення: -launcher вказує не на файл (%s) — працюю як звичайний агент", p)
		return
	}
	launcherPath = abs
	setUpdateNote(os.Getenv(envUpdateNote))
}

// runPayloadIfAny — робота запускача. Повертається, коли придатного оновлення
// немає (тоді викликач сам обслуговує верстат вбудованою версією). Якщо
// оновлена версія працює, не повертається: процес живе сторожем при ній і
// завершується разом з нею.
func runPayloadIfAny(cfgPath string, cfg Config) {
	root := updateRoot()
	if root == "" {
		return
	}
	keys, err := upd.ParseKeys(upd.TrustedKeys)
	if err != nil {
		log.Printf("оновлення: %v — самооновлення вимкнене", err)
		return
	}
	for {
		st := upd.LoadState(root)
		p, why := upd.Pick(root, buildStamp(), keys, st)
		if why != "" {
			setUpdateNote(why)
		}
		if p == nil {
			if why != "" {
				log.Printf("оновлення: найновіша версія не запущена: %s", why)
			}
			return
		}
		if runsElevated() {
			setUpdateNote("чекає наступного входу в Windows: агент запущено з правами адміністратора")
			log.Printf("оновлення %s є, але агент працює з підвищеними правами — лишаюсь на вбудованій до наступного входу", p.Manifest.Version)
			return
		}
		code, healthy, deadWhy, startErr := superviseOnce(p, cfgPath, cfg, why)
		switch {
		case startErr != nil:
			st.MarkBad(p.Manifest.SHA256, "не запустилась: "+startErr.Error())
			_ = st.Save(root)
			log.Printf("оновлення %s не запустилось: %v", p.Manifest.Version, startErr)
		case !healthy:
			st.MarkBad(p.Manifest.SHA256, deadWhy)
			_ = st.Save(root)
			log.Printf("оновлення %s: %s — позначено несправним", p.Manifest.Version, deadWhy)
		case code == 0:
			// Штатний вихід (користувач зупинив агента, або порт уже тримає
			// інший живий агент) — запускач іде слідом, а не воскрешає.
			os.Exit(0)
		case code == exitRestartForUpdate:
			log.Printf("оновлення: версія %s просить перечитати теку оновлень", p.Manifest.Version)
		default:
			if st.NoteCrash(p.Manifest.SHA256, time.Now()) {
				st.MarkBad(p.Manifest.SHA256, "падала кілька разів поспіль")
				log.Printf("оновлення %s впало (код %d) втретє за годину — позначено несправним", p.Manifest.Version, code)
			} else {
				log.Printf("оновлення %s впало (код %d) — запускаю знову", p.Manifest.Version, code)
			}
			_ = st.Save(root)
			time.Sleep(2 * time.Second)
		}
	}
}

// superviseOnce запускає оновлену версію й чекає її завершення.
// healthy=false — вона не почала відповідати (і вже зупинена); deadWhy каже,
// що саме сталось.
func superviseOnce(p *upd.Payload, cfgPath string, cfg Config, olderNote string) (code int, healthy bool, deadWhy string, startErr error) {
	cmd := exec.Command(p.Exe, "-serve", "-config", cfgPath, "-launcher", exePath())
	cmd.Dir = exeDir()
	cmd.Env = append(os.Environ(),
		envBuiltinVersion+"="+Version,
		envUpdateNote+"="+olderNote,
	)
	if err := cmd.Start(); err != nil {
		return 0, false, "", err
	}
	log.Printf("оновлення: запущено версію %s (%s)", p.Manifest.Version, p.Exe)

	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()

	deadline := time.Now().Add(payloadHealthWait)
	for !healthy {
		select {
		case err := <-done:
			// Вийшла ще до того, як почала відповідати. Єдина добра причина —
			// порт уже тримає інший живий агент (нас підняли зайвий раз).
			// Код виходу тут нічого не доводить: файл, що просто завершився
			// з нулем, агентом від цього не стає.
			if healthyAgentOn(cfg.Bind) {
				return 0, true, "", nil
			}
			return exitCode(err), false, "завершилась, не почавши відповідати (код " + strconv.Itoa(exitCode(err)) + ")", nil
		case <-time.After(payloadHealthStep):
		}
		if healthyAgentOn(cfg.Bind) {
			healthy = true
			break
		}
		if time.Now().After(deadline) {
			_ = cmd.Process.Kill()
			<-done
			return 0, false, "не відповіла за " + payloadHealthWait.String() + " після запуску", nil
		}
	}
	log.Printf("оновлення: версія %s відповідає — працює", p.Manifest.Version)
	// Довела, що жива, — попередні версії більше не потрібні.
	upd.Cleanup(filepath.Dir(p.Dir), p.Manifest.Built)
	return exitCode(<-done), true, "", nil
}

func exitCode(err error) int {
	if err == nil {
		return 0
	}
	var ee *exec.ExitError
	if errors.As(err, &ee) {
		return ee.ExitCode()
	}
	return 1
}

// handleUpdate приймає нову версію від CRM: маніфест і підпис у заголовках,
// файл — тілом запиту. Перевірка підпису стоїть ПЕРЕД записом на диск; токен
// агента сам по собі права оновлювати не дає — лише підпис.
func handleUpdate(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	fail := func(status int, msg string) {
		log.Printf("оновлення відхилено: %s", msg)
		w.WriteHeader(status)
		writeJSON(w, map[string]interface{}{"ok": false, "error": msg})
	}
	root := updateRoot()
	if root == "" {
		fail(http.StatusInternalServerError, "немає теки користувача для оновлень")
		return
	}
	keys, err := upd.ParseKeys(upd.TrustedKeys)
	if err != nil {
		fail(http.StatusInternalServerError, err.Error())
		return
	}
	rawManifest, err := base64.StdEncoding.DecodeString(r.Header.Get("X-Update-Manifest"))
	if err != nil || len(rawManifest) == 0 {
		fail(http.StatusBadRequest, "немає маніфесту")
		return
	}
	sig, err := hex.DecodeString(r.Header.Get("X-Update-Signature"))
	if err != nil {
		fail(http.StatusBadRequest, "підпис не читається")
		return
	}
	// Сервер знімків обмежує читання запиту 10 секундами — для кадру досить,
	// для файла на повільній мережі цеху ні.
	_ = http.NewResponseController(w).SetReadDeadline(time.Now().Add(updateReadDeadline))
	body := http.MaxBytesReader(w, r.Body, upd.MaxPayloadBytes)

	m, existed, err := upd.Store(root, rawManifest, sig, body, buildStamp(), keys)
	if err != nil {
		status := http.StatusBadRequest
		if errors.Is(err, upd.ErrNotNewer) {
			status = http.StatusConflict
		}
		fail(status, err.Error())
		return
	}
	resp := map[string]interface{}{
		"ok": true, "version": m.Version, "built": m.Built,
		"existed": existed, "restart": true, "note": "",
	}
	if runsElevated() {
		resp["restart"] = false
		resp["note"] = "нова версія запрацює після наступного входу в Windows: агент запущено з правами адміністратора"
		log.Printf("оновлення %s прийнято, чекає наступного входу в Windows", m.Version)
		writeJSON(w, resp)
		return
	}
	log.Printf("оновлення %s прийнято — перезапуск", m.Version)
	writeJSON(w, resp)
	go restartForUpdate()
}

// restartForUpdate віддає керування запускачу, щоб той підняв нову версію.
func restartForUpdate() {
	// Дати відповіді дійти до CRM.
	time.Sleep(700 * time.Millisecond)
	if isPayload() {
		// Над нами живе запускач-сторож: він побачить код і перечитає теку.
		os.Exit(exitRestartForUpdate)
	}
	// Ми — встановлений exe, що сам обслуговує верстат. Спершу звільнити
	// порт: новий процес, заставши тут живого агента, тихо вийшов би як
	// зайвий (див. healthyAgentOn), і оновлення чекало б сторожової задачі.
	if srv, ok := liveServer.Load().(*http.Server); ok && srv != nil {
		_ = srv.Close()
	}
	startDetachedServe()
	time.Sleep(300 * time.Millisecond)
	os.Exit(0)
}
