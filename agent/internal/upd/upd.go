// Package upd — самооновлення агента: підписаний маніфест, вибір придатної
// версії, облік невдалих запусків.
//
// Правило одне й воно не обговорюється: агент запускає ЛИШЕ файл, чий маніфест
// підписано нашим ключем, чий вміст збігається з маніфестом байт у байт і який
// новіший за вже встановлений. Усе інше — «оновлення немає». Жодної гілки
// «підпис не перевірився, але спробуймо» тут бути не може: цей код вирішує, що
// виконається на ПК верстата.
//
// Пакет не знає ні про HTTP, ні про процеси — лише файли й підпис, тому
// перевіряється тестами на будь-якій ОС.
package upd

import (
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
)

const (
	ProductName  = "kmill-agent"
	ExeName      = "kmill-agent.exe"
	ManifestName = "manifest.json"
	SigName      = "manifest.sig"
	stateName    = "state.json"

	// MaxPayloadBytes — стеля розміру файла оновлення. Агент важить ~7 МБ;
	// запас на порядок, а не «скільки пришлють».
	MaxPayloadBytes = 64 << 20

	// Три падіння за годину — версія несправна. Одне падіння на добу версію
	// не ховає: її підніме сторож, як і вбудовану.
	crashWindow = time.Hour
	crashLimit  = 3
)

// TrustedKeys — публічні ключі ed25519 (hex), якими може бути підписано
// оновлення. Приватна частина живе лише в секретах GitHub (AGENT_SIGNING_KEY)
// і підписує збірку в CI. Список, а не одне значення: зміна ключа — це агент,
// підписаний СТАРИМ ключем, у якому вже лежить новий.
var TrustedKeys = []string{
	"20bd5af8a0e0adfe4b42f3fe0fb4cb64bb1293cbafd5d0f469ad7cc446fca344",
}

// Manifest — те, що підписано. Підписуються САМЕ байти файла manifest.json,
// не перезібраний JSON: інакше порядок полів чи пробіл зламали б підпис.
type Manifest struct {
	Name    string `json:"name"`
	Version string `json:"version"`
	// Built — час збірки в секундах Unix. Саме ним версії порівнюються:
	// «2026.11.17-abc1234» упорядкувати не можна (хвіст — хеш коміту).
	Built  int64  `json:"built"`
	SHA256 string `json:"sha256"`
	Size   int64  `json:"size"`
}

// Payload — придатна до запуску версія на диску.
type Payload struct {
	Dir      string
	Exe      string
	Manifest Manifest
}

// ParseKeys перетворює hex-рядки на ключі. Битий ключ — помилка, а не
// «пропустити»: мовчки порожній список означав би «жодне оновлення не пройде»
// без жодного пояснення.
func ParseKeys(hexes []string) ([]ed25519.PublicKey, error) {
	var keys []ed25519.PublicKey
	for _, h := range hexes {
		raw, err := hex.DecodeString(strings.TrimSpace(h))
		if err != nil || len(raw) != ed25519.PublicKeySize {
			return nil, fmt.Errorf("битий публічний ключ %q", h)
		}
		keys = append(keys, ed25519.PublicKey(raw))
	}
	if len(keys) == 0 {
		return nil, errors.New("немає жодного довіреного ключа")
	}
	return keys, nil
}

// VerifyManifest перевіряє підпис СИРИХ байтів маніфесту і лише потім їх
// розбирає. Порядок важливий: непідписаний JSON не має права дійти до парсера.
func VerifyManifest(raw, sig []byte, keys []ed25519.PublicKey) (*Manifest, error) {
	if len(sig) != ed25519.SignatureSize {
		return nil, errors.New("підпис не того розміру")
	}
	signed := false
	for _, k := range keys {
		if ed25519.Verify(k, raw, sig) {
			signed = true
			break
		}
	}
	if !signed {
		return nil, errors.New("підпис не збігається")
	}
	var m Manifest
	if err := json.Unmarshal(raw, &m); err != nil {
		return nil, fmt.Errorf("маніфест не читається: %v", err)
	}
	if m.Name != ProductName {
		return nil, fmt.Errorf("маніфест не агента (%q)", m.Name)
	}
	if m.Built <= 0 || m.Size <= 0 || m.Size > MaxPayloadBytes || len(m.SHA256) != sha256.Size*2 {
		return nil, errors.New("маніфест неповний")
	}
	m.SHA256 = strings.ToLower(m.SHA256)
	return &m, nil
}

// Sign будує маніфест і підпис. Використовує лише збірка (tools/sign) і тести.
func Sign(seed []byte, m Manifest) (raw, sig []byte, err error) {
	if len(seed) != ed25519.SeedSize {
		return nil, nil, errors.New("ключ підпису не того розміру")
	}
	m.Name = ProductName
	raw, err = json.Marshal(m)
	if err != nil {
		return nil, nil, err
	}
	return raw, ed25519.Sign(ed25519.NewKeyFromSeed(seed), raw), nil
}

// HashFile рахує sha256 і розмір файла.
func HashFile(path string) (string, int64, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", 0, err
	}
	defer f.Close()
	h := sha256.New()
	n, err := io.Copy(h, f)
	if err != nil {
		return "", 0, err
	}
	return hex.EncodeToString(h.Sum(nil)), n, nil
}

// ---------------------------------------------------------------------------
// Облік: що несправне і скільки разів падало.
// ---------------------------------------------------------------------------

// State — пам'ять сторожа між запусками. Ключ — sha256 файла, а не версія:
// несправним є конкретний файл.
type State struct {
	Bad     map[string]string  `json:"bad"`
	Crashes map[string][]int64 `json:"crashes"`
}

// LoadState читає облік. Битий чи відсутній файл = порожній облік: гірше було
// б не запустити агента взагалі через зіпсований файл обліку.
func LoadState(root string) *State {
	st := &State{Bad: map[string]string{}, Crashes: map[string][]int64{}}
	raw, err := os.ReadFile(filepath.Join(root, stateName))
	if err != nil {
		return st
	}
	var loaded State
	if json.Unmarshal(raw, &loaded) != nil {
		return st
	}
	if loaded.Bad != nil {
		st.Bad = loaded.Bad
	}
	if loaded.Crashes != nil {
		st.Crashes = loaded.Crashes
	}
	return st
}

// Save пише облік через тимчасовий файл і перейменування.
func (s *State) Save(root string) error {
	raw, err := json.MarshalIndent(s, "", "  ")
	if err != nil {
		return err
	}
	tmp := filepath.Join(root, stateName+".tmp")
	if err := os.WriteFile(tmp, raw, 0644); err != nil {
		return err
	}
	return os.Rename(tmp, filepath.Join(root, stateName))
}

// MarkBad позначає файл несправним назавжди (до нової версії з іншим хешем).
func (s *State) MarkBad(sha, reason string) { s.Bad[sha] = reason }

// NoteCrash записує падіння й каже, чи версію час вважати несправною.
func (s *State) NoteCrash(sha string, now time.Time) bool {
	cutoff := now.Add(-crashWindow).Unix()
	var recent []int64
	for _, t := range s.Crashes[sha] {
		if t >= cutoff {
			recent = append(recent, t)
		}
	}
	recent = append(recent, now.Unix())
	s.Crashes[sha] = recent
	return len(recent) >= crashLimit
}

// ---------------------------------------------------------------------------
// Вибір версії для запуску.
// ---------------------------------------------------------------------------

// builtDirs — теки версій (назва = Built), найновіша перша.
func builtDirs(root string) []int64 {
	entries, err := os.ReadDir(root)
	if err != nil {
		return nil
	}
	var out []int64
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		n, err := strconv.ParseInt(e.Name(), 10, 64)
		if err != nil || n <= 0 || strconv.FormatInt(n, 10) != e.Name() {
			continue
		}
		out = append(out, n)
	}
	sort.Slice(out, func(i, j int) bool { return out[i] > out[j] })
	return out
}

// check перевіряє одну теку версії повністю: підпис, новизну, облік, вміст.
func check(root string, built, minBuilt int64, keys []ed25519.PublicKey, st *State) (*Payload, error) {
	dir := filepath.Join(root, strconv.FormatInt(built, 10))
	raw, err := os.ReadFile(filepath.Join(dir, ManifestName))
	if err != nil {
		return nil, errors.New("немає маніфесту")
	}
	sigHex, err := os.ReadFile(filepath.Join(dir, SigName))
	if err != nil {
		return nil, errors.New("немає підпису")
	}
	sig, err := hex.DecodeString(strings.TrimSpace(string(sigHex)))
	if err != nil {
		return nil, errors.New("підпис не читається")
	}
	m, err := VerifyManifest(raw, sig, keys)
	if err != nil {
		return nil, err
	}
	if m.Built != built {
		return nil, errors.New("тека не відповідає маніфесту")
	}
	if m.Built <= minBuilt {
		return nil, errors.New("не новіша за встановлену")
	}
	if why, bad := st.Bad[m.SHA256]; bad {
		return nil, fmt.Errorf("позначена несправною: %s", why)
	}
	exe := filepath.Join(dir, ExeName)
	sum, size, err := HashFile(exe)
	if err != nil {
		return nil, errors.New("немає файла програми")
	}
	if sum != m.SHA256 || size != m.Size {
		return nil, errors.New("файл не збігається з маніфестом")
	}
	return &Payload{Dir: dir, Exe: exe, Manifest: *m}, nil
}

// Pick повертає найновішу придатну версію або nil. Другий результат — чому
// найновіша з наявних НЕ підійшла (порожньо, якщо оновлень немає взагалі або
// обрано саме найновішу): це єдине, що побачить людина в CRM.
func Pick(root string, minBuilt int64, keys []ed25519.PublicKey, st *State) (*Payload, string) {
	why := ""
	for i, built := range builtDirs(root) {
		p, err := check(root, built, minBuilt, keys, st)
		if err == nil {
			return p, why
		}
		if i == 0 {
			why = err.Error()
		}
	}
	return nil, why
}

// Cleanup прибирає теки версій, старіших за keep, і покинуті теки прийому.
// Кличеться лише коли keep уже довела, що працює: до того попередня версія —
// запасний варіант.
func Cleanup(root string, keep int64) {
	for _, built := range builtDirs(root) {
		if built < keep {
			_ = os.RemoveAll(filepath.Join(root, strconv.FormatInt(built, 10)))
		}
	}
	entries, _ := os.ReadDir(root)
	for _, e := range entries {
		if e.IsDir() && strings.HasPrefix(e.Name(), incomingPrefix) {
			if info, err := e.Info(); err == nil && time.Since(info.ModTime()) > time.Hour {
				_ = os.RemoveAll(filepath.Join(root, e.Name()))
			}
		}
	}
}

// ---------------------------------------------------------------------------
// Прийом нової версії.
// ---------------------------------------------------------------------------

const incomingPrefix = "incoming-"

// ErrNotNewer — прислали версію, не новішу за ту, що вже працює.
var ErrNotNewer = errors.New("агент уже має цю або новішу версію")

// Store приймає нову версію: спершу підпис маніфесту, потім вміст у тимчасову
// теку зі звіркою хешу, і лише тоді тека отримує справжню назву. Файл, що не
// пройшов перевірку, під справжньою назвою не існує ні миті.
//
// existed=true — така версія вже лежить на диску (повторне надсилання).
func Store(root string, rawManifest, sig []byte, body io.Reader, minBuilt int64, keys []ed25519.PublicKey) (m *Manifest, existed bool, err error) {
	m, err = VerifyManifest(rawManifest, sig, keys)
	if err != nil {
		return nil, false, err
	}
	if m.Built <= minBuilt {
		return nil, false, ErrNotNewer
	}
	if err := os.MkdirAll(root, 0755); err != nil {
		return nil, false, err
	}
	final := filepath.Join(root, strconv.FormatInt(m.Built, 10))
	if _, statErr := os.Stat(final); statErr == nil {
		// Уже є. Довіряти чи ні — вирішить Pick при запуску, тут не перезаписуємо:
		// файл може бути запущений.
		_, _ = io.Copy(io.Discard, io.LimitReader(body, MaxPayloadBytes))
		return m, true, nil
	}
	tmp, err := os.MkdirTemp(root, incomingPrefix)
	if err != nil {
		return nil, false, err
	}
	defer func() {
		if err != nil {
			_ = os.RemoveAll(tmp)
		}
	}()
	out, err := os.OpenFile(filepath.Join(tmp, ExeName), os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0755)
	if err != nil {
		return nil, false, err
	}
	h := sha256.New()
	// На байт більше за заявлене: довший файл мусить дати помилку, а не бути
	// мовчки обрізаним до «правильного» розміру.
	n, copyErr := io.Copy(io.MultiWriter(out, h), io.LimitReader(body, m.Size+1))
	closeErr := out.Close()
	if copyErr != nil {
		return nil, false, copyErr
	}
	if closeErr != nil {
		return nil, false, closeErr
	}
	if n != m.Size || hex.EncodeToString(h.Sum(nil)) != m.SHA256 {
		return nil, false, errors.New("файл не збігається з маніфестом")
	}
	if err = os.WriteFile(filepath.Join(tmp, ManifestName), rawManifest, 0644); err != nil {
		return nil, false, err
	}
	if err = os.WriteFile(filepath.Join(tmp, SigName), []byte(hex.EncodeToString(sig)), 0644); err != nil {
		return nil, false, err
	}
	if err = os.Rename(tmp, final); err != nil {
		return nil, false, err
	}
	return m, false, nil
}
