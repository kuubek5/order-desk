package upd

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/hex"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

// Тестовий ключ: справжній приватний ключ у репозиторії не лежить і лежати
// не може, тому тести підписують своїм і передають його як довірений.
func testKey(t *testing.T, fill byte) ([]byte, []ed25519.PublicKey) {
	t.Helper()
	seed := bytes.Repeat([]byte{fill}, ed25519.SeedSize)
	pub := ed25519.NewKeyFromSeed(seed).Public().(ed25519.PublicKey)
	return seed, []ed25519.PublicKey{pub}
}

func signed(t *testing.T, seed []byte, built int64, body []byte) (raw, sig []byte) {
	t.Helper()
	sum := sha256.Sum256(body)
	raw, sig, err := Sign(seed, Manifest{
		Version: "2026.11.17-test", Built: built,
		SHA256: hex.EncodeToString(sum[:]), Size: int64(len(body)),
	})
	if err != nil {
		t.Fatal(err)
	}
	return raw, sig
}

func store(t *testing.T, root string, seed []byte, keys []ed25519.PublicKey, built int64, body []byte) {
	t.Helper()
	raw, sig := signed(t, seed, built, body)
	if _, _, err := Store(root, raw, sig, bytes.NewReader(body), 0, keys); err != nil {
		t.Fatalf("store %d: %v", built, err)
	}
}

func TestTrustedKeysParse(t *testing.T) {
	// Битий ключ у списку зробив би КОЖНЕ оновлення «непідписаним» без
	// пояснення — ловимо на збірці, а не в цеху.
	if _, err := ParseKeys(TrustedKeys); err != nil {
		t.Fatal(err)
	}
	if _, err := ParseKeys([]string{"zz"}); err == nil {
		t.Fatal("битий ключ прийнято")
	}
	if _, err := ParseKeys(nil); err == nil {
		t.Fatal("порожній список ключів прийнято")
	}
}

func TestStoreThenPick(t *testing.T) {
	root := t.TempDir()
	seed, keys := testKey(t, 1)
	body := []byte("new agent")
	store(t, root, seed, keys, 200, body)

	p, why := Pick(root, 100, keys, LoadState(root))
	if p == nil {
		t.Fatalf("версію не обрано: %s", why)
	}
	if p.Manifest.Built != 200 || filepath.Base(p.Exe) != ExeName {
		t.Fatalf("обрано не те: %+v", p)
	}
	got, _ := os.ReadFile(p.Exe)
	if !bytes.Equal(got, body) {
		t.Fatal("вміст файла змінився по дорозі")
	}
}

func TestUnsignedIsRefusedEverywhere(t *testing.T) {
	root := t.TempDir()
	seed, keys := testKey(t, 1)
	otherSeed, _ := testKey(t, 2)
	body := []byte("agent")

	// Підписано чужим ключем.
	raw, sig := signed(t, otherSeed, 200, body)
	if _, _, err := Store(root, raw, sig, bytes.NewReader(body), 0, keys); err == nil {
		t.Fatal("чужий підпис прийнято")
	}
	// Маніфест змінено після підпису.
	raw, sig = signed(t, seed, 200, body)
	tampered := bytes.Replace(raw, []byte(`"built":200`), []byte(`"built":900`), 1)
	if bytes.Equal(tampered, raw) {
		t.Fatal("тест не змінив маніфест")
	}
	if _, _, err := Store(root, tampered, sig, bytes.NewReader(body), 0, keys); err == nil {
		t.Fatal("змінений маніфест прийнято")
	}
	// Після відмов на диску не лишилось нічого, що можна запустити.
	if p, _ := Pick(root, 0, keys, LoadState(root)); p != nil {
		t.Fatal("після відмов знайшлась версія")
	}
	left, _ := os.ReadDir(root)
	if len(left) != 0 {
		t.Fatalf("після відмов на диску лишилось: %v", left)
	}
}

func TestBodyMustMatchManifest(t *testing.T) {
	root := t.TempDir()
	seed, keys := testKey(t, 1)
	body := []byte("agent body")
	raw, sig := signed(t, seed, 200, body)

	for name, sent := range map[string][]byte{
		"інший вміст": []byte("agent b0dy"),
		"довший":      append(append([]byte{}, body...), 'x'),
		"коротший":    body[:len(body)-1],
	} {
		if _, _, err := Store(root, raw, sig, bytes.NewReader(sent), 0, keys); err == nil {
			t.Fatalf("%s: прийнято", name)
		}
	}
	left, _ := os.ReadDir(root)
	if len(left) != 0 {
		t.Fatalf("невдалий прийом лишив файли: %v", left)
	}
}

func TestNoDowngrade(t *testing.T) {
	root := t.TempDir()
	seed, keys := testKey(t, 1)
	body := []byte("old agent")
	raw, sig := signed(t, seed, 200, body)

	// Та сама й старша версія — відмова ще до запису.
	for _, min := range []int64{200, 300} {
		if _, _, err := Store(root, raw, sig, bytes.NewReader(body), min, keys); err != ErrNotNewer {
			t.Fatalf("min=%d: %v", min, err)
		}
	}
	// Лежить на диску, але вбудована версія новіша (поставили свіжий
	// інсталятор руками) — не запускаємо.
	store(t, root, seed, keys, 200, body)
	if p, why := Pick(root, 500, keys, LoadState(root)); p != nil || why == "" {
		t.Fatalf("старішу за вбудовану обрано: %+v %q", p, why)
	}
}

func TestFileChangedAfterStoreIsRefused(t *testing.T) {
	root := t.TempDir()
	seed, keys := testKey(t, 1)
	store(t, root, seed, keys, 200, []byte("agent"))
	exe := filepath.Join(root, "200", ExeName)
	if err := os.WriteFile(exe, []byte("evil!"), 0755); err != nil {
		t.Fatal(err)
	}
	p, why := Pick(root, 0, keys, LoadState(root))
	if p != nil {
		t.Fatal("підмінений файл обрано")
	}
	if !strings.Contains(why, "не збігається") {
		t.Fatalf("причина не названа: %q", why)
	}
}

func TestFolderNameCannotLie(t *testing.T) {
	// Тека «900» з маніфестом версії 200: назва теки не має права підняти
	// стару версію над новою.
	root := t.TempDir()
	seed, keys := testKey(t, 1)
	store(t, root, seed, keys, 200, []byte("agent"))
	if err := os.Rename(filepath.Join(root, "200"), filepath.Join(root, "900")); err != nil {
		t.Fatal(err)
	}
	if p, _ := Pick(root, 0, keys, LoadState(root)); p != nil {
		t.Fatal("теку з чужою назвою обрано")
	}
}

func TestBadVersionFallsBackToPrevious(t *testing.T) {
	root := t.TempDir()
	seed, keys := testKey(t, 1)
	store(t, root, seed, keys, 200, []byte("good agent"))
	store(t, root, seed, keys, 300, []byte("broken agent"))

	st := LoadState(root)
	p, _ := Pick(root, 100, keys, st)
	if p == nil || p.Manifest.Built != 300 {
		t.Fatalf("спершу мала бути найновіша: %+v", p)
	}
	st.MarkBad(p.Manifest.SHA256, "не відповіла")
	if err := st.Save(root); err != nil {
		t.Fatal(err)
	}

	// Облік переживає перезапуск, і вибір відступає на попередню.
	p, why := Pick(root, 100, keys, LoadState(root))
	if p == nil || p.Manifest.Built != 200 {
		t.Fatalf("мала бути попередня: %+v", p)
	}
	if !strings.Contains(why, "не відповіла") {
		t.Fatalf("причина відмови найновішої загубилась: %q", why)
	}
	// Попередня теж несправна — лишається вбудована.
	st = LoadState(root)
	st.MarkBad(p.Manifest.SHA256, "теж")
	if p, _ := Pick(root, 100, keys, st); p != nil {
		t.Fatal("обидві несправні, а версію обрано")
	}
}

func TestCrashCounting(t *testing.T) {
	st := LoadState(t.TempDir())
	now := time.Unix(1_000_000, 0)
	if st.NoteCrash("a", now) || st.NoteCrash("a", now.Add(time.Minute)) {
		t.Fatal("несправною після двох падінь")
	}
	if !st.NoteCrash("a", now.Add(2*time.Minute)) {
		t.Fatal("три падіння за годину — мала стати несправною")
	}
	// Рідкі падіння версію не ховають.
	if st.NoteCrash("b", now) || st.NoteCrash("b", now.Add(2*time.Hour)) || st.NoteCrash("b", now.Add(4*time.Hour)) {
		t.Fatal("три падіння за чотири години — це не привід")
	}
}

func TestBrokenStateFileDoesNotBlock(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, stateName), []byte("{not json"), 0644); err != nil {
		t.Fatal(err)
	}
	st := LoadState(root)
	if st.Bad == nil || st.Crashes == nil {
		t.Fatal("битий облік дав nil-мапи")
	}
}

func TestStoreSameVersionTwice(t *testing.T) {
	root := t.TempDir()
	seed, keys := testKey(t, 1)
	body := []byte("agent")
	raw, sig := signed(t, seed, 200, body)
	if _, existed, err := Store(root, raw, sig, bytes.NewReader(body), 0, keys); err != nil || existed {
		t.Fatalf("перший раз: %v %v", existed, err)
	}
	if _, existed, err := Store(root, raw, sig, bytes.NewReader(body), 0, keys); err != nil || !existed {
		t.Fatalf("другий раз: %v %v", existed, err)
	}
}

func TestCleanupKeepsCurrentAndNewer(t *testing.T) {
	root := t.TempDir()
	seed, keys := testKey(t, 1)
	for _, b := range []int64{100, 200, 300} {
		store(t, root, seed, keys, b, []byte("agent "+strconv.FormatInt(b, 10)))
	}
	Cleanup(root, 200)
	for name, want := range map[string]bool{"100": false, "200": true, "300": true} {
		_, err := os.Stat(filepath.Join(root, name))
		if (err == nil) != want {
			t.Fatalf("тека %s: є=%v, треба=%v", name, err == nil, want)
		}
	}
}
