//go:build e2e && windows

package main

// Наскрізний тест самооновлення: справжні процеси, справжній HTTP, справжня
// підміна версії й відкат. Запуск вручну:
//
//	go test -tags e2e -run E2E -v -timeout 10m .
//
// Збирає агента тричі (встановлений v1, оновлення v2 і v3) з тестовим ключем
// (internal/upd/keys_e2e.go), запускає «встановлений» у тимчасовій теці на
// 127.0.0.1 (без запиту брандмауера) і з власною текою LOCALAPPDATA.

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"kmill-agent/internal/upd"
)

const (
	e2ePort  = "18765"
	e2eToken = "e2e-token"
)

var e2eSeed = bytes.Repeat([]byte{7}, ed25519.SeedSize)

func e2eBuild(t *testing.T, out, version string, built int64) {
	t.Helper()
	cmd := exec.Command("go", "build", "-tags", "e2e",
		"-ldflags", fmt.Sprintf("-H=windowsgui -X main.Version=%s -X main.BuildStamp=%d", version, built),
		"-o", out, ".")
	if b, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("build %s: %v\n%s", version, err, b)
	}
}

func e2eStatus() map[string]interface{} {
	req, _ := http.NewRequest("GET", "http://127.0.0.1:"+e2ePort+"/status", nil)
	req.Header.Set("X-Agent-Token", e2eToken)
	resp, err := (&http.Client{Timeout: 3 * time.Second}).Do(req)
	if err != nil {
		return nil
	}
	defer resp.Body.Close()
	var out map[string]interface{}
	if json.NewDecoder(resp.Body).Decode(&out) != nil {
		return nil
	}
	return out
}

// e2eWait чекає, поки /status задовольнить умову.
func e2eWait(t *testing.T, what string, limit time.Duration, ok func(map[string]interface{}) bool) map[string]interface{} {
	t.Helper()
	deadline := time.Now().Add(limit)
	var last map[string]interface{}
	for time.Now().Before(deadline) {
		if last = e2eStatus(); last != nil && ok(last) {
			return last
		}
		time.Sleep(500 * time.Millisecond)
	}
	t.Fatalf("не дочекались: %s; останній /status: %v", what, last)
	return nil
}

func e2ePush(t *testing.T, seed []byte, version string, built int64, body []byte, tamper bool) (int, map[string]interface{}) {
	t.Helper()
	sum := sha256.Sum256(body)
	raw, sig, err := upd.Sign(seed, upd.Manifest{
		Version: version, Built: built, SHA256: hex.EncodeToString(sum[:]), Size: int64(len(body)),
	})
	if err != nil {
		t.Fatal(err)
	}
	if tamper {
		body = append([]byte{}, body...)
		body[len(body)/2] ^= 0xff
	}
	req, _ := http.NewRequest("POST", "http://127.0.0.1:"+e2ePort+"/update", bytes.NewReader(body))
	req.Header.Set("X-Agent-Token", e2eToken)
	req.Header.Set("X-Update-Manifest", base64.StdEncoding.EncodeToString(raw))
	req.Header.Set("X-Update-Signature", hex.EncodeToString(sig))
	resp, err := (&http.Client{Timeout: 60 * time.Second}).Do(req)
	if err != nil {
		t.Fatalf("push %s: %v", version, err)
	}
	defer resp.Body.Close()
	var out map[string]interface{}
	b, _ := io.ReadAll(resp.Body)
	_ = json.Unmarshal(b, &out)
	return resp.StatusCode, out
}

func e2eKill(pid float64) {
	_ = exec.Command("taskkill", "/F", "/PID", strconv.Itoa(int(pid))).Run()
}

func TestE2ESelfUpdate(t *testing.T) {
	work, err := os.MkdirTemp("", "kmill-e2e-")
	if err != nil {
		t.Fatal(err)
	}
	// Після провалу тека лишається: у ній лог запускача, без якого не видно,
	// чому він вирішив саме так.
	defer func() {
		if t.Failed() {
			t.Logf("тека тесту збережена: %s", work)
			return
		}
		_ = os.RemoveAll(work)
	}()
	installed := filepath.Join(work, "installed")
	local := filepath.Join(work, "localappdata")
	for _, d := range []string{installed, local} {
		if err := os.MkdirAll(d, 0755); err != nil {
			t.Fatal(err)
		}
	}
	launcher := filepath.Join(installed, "kmill-agent.exe")
	v2 := filepath.Join(work, "v2.exe")
	v3 := filepath.Join(work, "v3.exe")
	e2eBuild(t, launcher, "e2e-v1", 100)
	e2eBuild(t, v2, "e2e-v2", 200)
	e2eBuild(t, v3, "e2e-v3", 300)
	cfg := fmt.Sprintf(`{"bind":"127.0.0.1:%s","token":"%s","display":0,"name":"e2e"}`, e2ePort, e2eToken)
	if err := os.WriteFile(filepath.Join(installed, "agent.json"), []byte(cfg), 0644); err != nil {
		t.Fatal(err)
	}

	cmd := exec.Command(launcher, "-serve")
	cmd.Dir = installed
	cmd.Env = append(os.Environ(), "LOCALAPPDATA="+local, "ProgramData="+filepath.Join(work, "programdata"))
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	// Прибирання — за шляхом файла, а не за портом: запускач, що стежить за
	// оновленою версією, уже не той процес, який стартував тест (перший сам
	// поступився новому), і вбита версія воскресла б під ним. Тому спершу всі
	// процеси з теки «встановленого», потім решта з теки тесту.
	defer func() {
		_ = cmd.Process.Kill()
		for _, under := range []string{installed, work} {
			e2eKillUnder(under)
			time.Sleep(time.Second)
		}
	}()

	// 1. Оновлень немає — працює встановлена версія.
	st := e2eWait(t, "встановлена v1", 20*time.Second, func(s map[string]interface{}) bool { return s["version"] == "e2e-v1" })
	if st["updated"] != false || st["self_update"] != true {
		t.Fatalf("v1: %v", st)
	}

	// 2. Чужий ключ і зіпсований по дорозі файл — відмова, версія та сама.
	body2, _ := os.ReadFile(v2)
	otherSeed := bytes.Repeat([]byte{9}, ed25519.SeedSize)
	if code, _ := e2ePush(t, otherSeed, "e2e-v2", 200, body2, false); code != http.StatusBadRequest {
		t.Fatalf("чужий підпис: код %d", code)
	}
	if code, _ := e2ePush(t, e2eSeed, "e2e-v2", 200, body2, true); code != http.StatusBadRequest {
		t.Fatalf("зіпсований файл: код %d", code)
	}
	time.Sleep(2 * time.Second)
	if st = e2eStatus(); st == nil || st["version"] != "e2e-v1" {
		t.Fatalf("після відмов версія змінилась: %v", st)
	}

	// 3. Справжнє оновлення до v2: агент сам перезапускається в нову версію.
	if code, out := e2ePush(t, e2eSeed, "e2e-v2", 200, body2, false); code != http.StatusOK || out["restart"] != true {
		t.Fatalf("оновлення v2: код %d, %v", code, out)
	}
	st = e2eWait(t, "оновлена v2", 30*time.Second, func(s map[string]interface{}) bool { return s["version"] == "e2e-v2" })
	if st["updated"] != true || st["builtin_version"] != "e2e-v1" {
		t.Fatalf("v2: %v", st)
	}

	// 4. Та сама версія вдруге — відмова (409), версія та сама.
	if code, _ := e2ePush(t, e2eSeed, "e2e-v2", 200, body2, false); code != http.StatusConflict {
		t.Fatalf("повтор тієї самої версії: код %d", code)
	}

	// 5. Оновлена версія впала — запускач піднімає її знову.
	//    Спершу дати запускачу підтвердити, що версія жива: падіння ДО цього
	//    він слушно читає як «не запрацювала» і відкочується (крок 7).
	time.Sleep(6 * time.Second)
	pidBefore := st["pid"].(float64)
	e2eKill(pidBefore)
	e2eWait(t, "v2 після падіння", 30*time.Second, func(s map[string]interface{}) bool {
		return s["version"] == "e2e-v2" && s["pid"].(float64) != pidBefore
	})

	// 6. Оновлення поверх оновлення: v2 → v3.
	body3, _ := os.ReadFile(v3)
	if code, out := e2ePush(t, e2eSeed, "e2e-v3", 300, body3, false); code != http.StatusOK {
		t.Fatalf("оновлення v3: код %d, %v", code, out)
	}
	e2eWait(t, "оновлена v3", 30*time.Second, func(s map[string]interface{}) bool { return s["version"] == "e2e-v3" })
	// Попередня версія прибрана після того, як нова довела, що працює.
	root := filepath.Join(local, "KMill Agent", "update")
	e2eWaitGone(t, filepath.Join(root, "200"))

	// 7. Несправне оновлення (підписане, але це не агент: виходить одразу) —
	//    запускач позначає його несправним і лишає v3; причина видна в /status.
	junk, err := os.ReadFile(filepath.Join(os.Getenv("SystemRoot"), "System32", "whoami.exe"))
	if err != nil {
		t.Fatal(err)
	}
	if code, out := e2ePush(t, e2eSeed, "e2e-junk", 400, junk, false); code != http.StatusOK {
		t.Fatalf("несправне оновлення: код %d, %v", code, out)
	}
	st = e2eWait(t, "відкат на v3 з причиною", 90*time.Second, func(s map[string]interface{}) bool {
		note, _ := s["update_note"].(string)
		return s["version"] == "e2e-v3" && strings.Contains(note, "несправн")
	})
	t.Logf("після несправного оновлення: версія %v, причина: %v", st["version"], st["update_note"])
}

func e2eWaitGone(t *testing.T, path string) {
	t.Helper()
	for i := 0; i < 20; i++ {
		if _, err := os.Stat(path); os.IsNotExist(err) {
			return
		}
		time.Sleep(500 * time.Millisecond)
	}
	t.Fatalf("тека попередньої версії не прибрана: %s", path)
}

// e2eKillUnder зупиняє процеси, чий exe лежить у теці тесту.
func e2eKillUnder(dir string) {
	script := "Get-Process kmill-agent -ErrorAction SilentlyContinue | " +
		"Where-Object { $_.Path -like '" + strings.ReplaceAll(dir, "'", "''") + `\*' } | Stop-Process -Force`
	_ = exec.Command("powershell", "-NoProfile", "-Command", script).Run()
}
