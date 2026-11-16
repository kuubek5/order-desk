package main

// Розвідка файлів на ПК верстата — ЛИШЕ ЧИТАННЯ (власник 14.11.26: знайти, де
// програма верстата тримає мотогодини фрез, заміни, температуру).
//
// Що агент робить:
//   GET /discover          -> перелік файлів (шлях, розмір, час зміни) у теках
//                             програм верстата; вмісту не читає
//   GET /discover?tops=1   -> лише назви тек верхнього рівня (щоб побачити, як
//                             тека програми зветься на цьому ПК)
//   GET /peek?path=...     -> початок ОДНОГО текстового файлу з дозволеної теки
//   GET /peek?...&tail=1   -> його кінець (свіже в журналі лежить там)
//   GET /peek?path=x.zip   -> перелік файлів архіву старих журналів;
//                             &entry=назва[&tail=1] — один текстовий файл з
//                             архіву, розпакований лише в пам'ять
//
// Чого він НЕ робить і не вміє за конструкцією:
//   * нічого не пише, не перейменовує й не видаляє — у цьому файлі немає
//     жодного виклику запису;
//   * не тримає файл верстата замкненим: читає зі спільним доступом на
//     читання, запис І видалення (openShared), тож програма верстата може
//     писати й прибирати свої файли, поки ми дивимось;
//   * не ходить поза теки програм верстата (allowedRoots): це не файловий
//     сервер. Власна тека агента з токеном не віддається ніколи;
//   * не навантажує ПК: один обхід за раз, паузи між теками, межа часу й
//     кількості файлів.

import (
	"archive/zip"
	"encoding/base64"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
)

const (
	discoverMaxFiles   = 4000
	discoverMaxDepth   = 7
	discoverDeadline   = 20 * time.Second
	discoverPauseEvery = 64
	discoverPause      = 3 * time.Millisecond
	peekMaxFileBytes   = 8 << 20
	peekMaxBytes       = 256 << 10
	// З кінця файлу читаємо ті самі peekMaxBytes, тож розмір самого журналу
	// не важить — межа лише від явно чужих велетнів.
	tailMaxFileBytes   = 4 << 30
	zipMaxFileBytes    = 256 << 20
	zipMaxEntryBytes   = 128 << 20
	zipMaxEntries      = 2000
)

// Слова в назві теки, за якими впізнаємо програму верстата.
var machineWords = []string{"imes", "icore", "coritec", "remicore", "cnc", "isel", "dental", "mill"}

// Теки, в які не заходимо навіть усередині дозволеної.
var skipDirs = map[string]bool{
	"windows": true, "$recycle.bin": true, "system volume information": true,
	"winsxs": true, "kmill agent": true,
}

// Розширення, які /peek погоджується відкрити: налаштування й журнали.
var textExt = map[string]bool{
	".ini": true, ".cfg": true, ".conf": true, ".config": true, ".log": true,
	".txt": true, ".xml": true, ".json": true, ".csv": true, ".tab": true,
	".dat": true, ".prm": true, ".par": true, ".lst": true, ".properties": true,
	".yaml": true, ".yml": true, ".tol": true, ".tool": true,
}

type dirEntry struct {
	Name  string
	IsDir bool
	Skip  bool // посилання/точка з'єднання — не заходимо, щоб не ходити колом
	Size  int64
	MTime time.Time
}

type fileEntry struct {
	Path  string `json:"path"`
	Size  int64  `json:"size"`
	MTime string `json:"mtime"`
}

type lister func(path string) ([]dirEntry, error)

func hasMachineWord(name string) bool {
	low := strings.ToLower(name)
	for _, word := range machineWords {
		if strings.Contains(low, word) {
			return true
		}
	}
	return false
}

func samePath(a, b string) bool {
	return strings.EqualFold(filepath.Clean(a), filepath.Clean(b))
}

// within: чи лежить path у котрійсь із тек roots (або є нею).
func within(path string, roots []string) bool {
	if !filepath.IsAbs(path) {
		return false
	}
	clean := strings.ToLower(filepath.Clean(path))
	for _, root := range roots {
		base := strings.ToLower(filepath.Clean(root))
		if clean == base || strings.HasPrefix(clean, strings.TrimRight(base, `\/`)+string(filepath.Separator)) {
			return true
		}
	}
	return false
}

// parentDirs: де шукаємо теки програм верстата — корені дисків і типові
// місця встановлення.
func parentDirs() []string {
	out := append([]string{}, fixedDrives()...)
	for _, key := range []string{"ProgramFiles", "ProgramFiles(x86)", "ProgramData", "LOCALAPPDATA", "APPDATA", "PUBLIC"} {
		if dir := os.Getenv(key); dir != "" {
			out = append(out, dir)
		}
	}
	return out
}

// allowedRoots: теки з назвою програми верстата безпосередньо в parents.
func allowedRoots(parents []string, list lister, own string) []string {
	var roots []string
	for _, parent := range parents {
		entries, err := list(parent)
		if err != nil {
			continue
		}
		for _, e := range entries {
			if !e.IsDir || e.Skip || skipDirs[strings.ToLower(e.Name)] || !hasMachineWord(e.Name) {
				continue
			}
			full := filepath.Join(parent, e.Name)
			if own != "" && samePath(full, own) {
				continue
			}
			roots = append(roots, full)
		}
	}
	sort.Strings(roots)
	return roots
}

// topNames: назви тек верхнього рівня в parents — без заходу всередину.
func topNames(parents []string, list lister) map[string][]string {
	out := map[string][]string{}
	for _, parent := range parents {
		entries, err := list(parent)
		if err != nil {
			continue
		}
		names := []string{}
		for _, e := range entries {
			if e.IsDir && !e.Skip {
				names = append(names, e.Name)
			}
		}
		sort.Strings(names)
		out[parent] = names
	}
	return out
}

// walkRoots обходить теки вшир: до discoverMaxDepth рівнів, discoverMaxFiles
// файлів і до deadline. Повертає файли, кількість пройдених тек і ознаку
// «не все влізло».
func walkRoots(roots []string, list lister, deadline time.Time, own string) ([]fileEntry, int, bool) {
	type step struct {
		path  string
		depth int
	}
	queue := make([]step, 0, len(roots))
	for _, root := range roots {
		queue = append(queue, step{root, 0})
	}
	files := []fileEntry{}
	dirs, seen := 0, 0
	for len(queue) > 0 {
		if time.Now().After(deadline) {
			return files, dirs, true
		}
		current := queue[0]
		queue = queue[1:]
		entries, err := list(current.path)
		if err != nil {
			continue
		}
		dirs++
		for _, e := range entries {
			seen++
			if seen%discoverPauseEvery == 0 {
				time.Sleep(discoverPause)
			}
			full := filepath.Join(current.path, e.Name)
			if e.IsDir {
				if e.Skip || skipDirs[strings.ToLower(e.Name)] || current.depth+1 > discoverMaxDepth {
					continue
				}
				if own != "" && samePath(full, own) {
					continue
				}
				queue = append(queue, step{full, current.depth + 1})
				continue
			}
			if len(files) >= discoverMaxFiles {
				return files, dirs, true
			}
			files = append(files, fileEntry{Path: full, Size: e.Size, MTime: e.MTime.Format(time.RFC3339)})
		}
	}
	return files, dirs, false
}

// Один обхід за раз: другий запит не стає в чергу, а одразу чує «зайнято».
var discoverBusy sync.Mutex

func handleDiscover(w http.ResponseWriter, r *http.Request) {
	if isPaused() {
		http.Error(w, "спостереження призупинено на верстаті", http.StatusServiceUnavailable)
		return
	}
	if !discoverBusy.TryLock() {
		http.Error(w, "обхід уже йде", http.StatusTooManyRequests)
		return
	}
	defer discoverBusy.Unlock()
	parents := parentDirs()
	if r.URL.Query().Get("tops") != "" {
		writeJSON(w, map[string]interface{}{"tops": topNames(parents, listDir)})
		return
	}
	started := time.Now()
	own := exeDir()
	roots := allowedRoots(parents, listDir, own)
	files, dirs, truncated := walkRoots(roots, listDir, started.Add(discoverDeadline), own)
	writeJSON(w, map[string]interface{}{
		"roots":      roots,
		"files":      files,
		"dirs":       dirs,
		"truncated":  truncated,
		"elapsed_ms": time.Since(started).Milliseconds(),
	})
}

func handlePeek(w http.ResponseWriter, r *http.Request) {
	if isPaused() {
		http.Error(w, "спостереження призупинено на верстаті", http.StatusServiceUnavailable)
		return
	}
	q := r.URL.Query()
	path := filepath.Clean(q.Get("path"))
	own := exeDir()
	if !within(path, allowedRoots(parentDirs(), listDir, own)) || (own != "" && within(path, []string{own})) {
		http.Error(w, "поза теками програми верстата", http.StatusForbidden)
		return
	}
	ext := strings.ToLower(filepath.Ext(path))
	isZip := ext == ".zip"
	if !textExt[ext] && !isZip {
		http.Error(w, "не текстовий файл налаштувань чи журналу", http.StatusForbidden)
		return
	}
	info, err := os.Stat(path)
	if err != nil || info.IsDir() {
		http.Error(w, "файлу немає", http.StatusNotFound)
		return
	}
	tail := q.Get("tail") != ""
	limitSize := int64(peekMaxFileBytes)
	if isZip {
		limitSize = zipMaxFileBytes
	} else if tail {
		limitSize = tailMaxFileBytes
	}
	if info.Size() > limitSize {
		http.Error(w, "файл завеликий", http.StatusRequestEntityTooLarge)
		return
	}
	f, err := openShared(path)
	if err != nil {
		http.Error(w, "не відкрився", http.StatusConflict)
		return
	}
	defer f.Close()
	if isZip {
		peekZip(w, f, path, info, q.Get("entry"), tail)
		return
	}
	data, offset, err := readPart(f, info.Size(), tail)
	if err != nil {
		http.Error(w, "не прочитався", http.StatusConflict)
		return
	}
	writeJSON(w, map[string]interface{}{
		"path":      path,
		"size":      info.Size(),
		"mtime":     info.ModTime().Format(time.RFC3339),
		"truncated": info.Size() > int64(len(data)),
		"tail":      tail,
		"offset":    offset,
		"data":      base64.StdEncoding.EncodeToString(data),
	})
}

// readPart: початок файлу або, з tail, його ОСТАННІ peekMaxBytes — журнал
// дописується в кінець, і свіже лежить саме там (власник 16.11.26: «потрібно
// максимум»). Повертає зсув, з якого прочитано.
func readPart(f io.ReadSeeker, size int64, tail bool) ([]byte, int64, error) {
	var offset int64
	if tail && size > peekMaxBytes {
		offset = size - peekMaxBytes
		if _, err := f.Seek(offset, io.SeekStart); err != nil {
			return nil, 0, err
		}
	}
	data, err := io.ReadAll(io.LimitReader(f, peekMaxBytes))
	return data, offset, err
}

// lastBytes читає потік до кінця (не більше max байт), лишаючи останні keep.
func lastBytes(rd io.Reader, keep int, max int64) ([]byte, int64, error) {
	buf := make([]byte, 0, keep)
	chunk := make([]byte, 32<<10)
	var total int64
	limited := io.LimitReader(rd, max)
	for {
		n, err := limited.Read(chunk)
		if n > 0 {
			total += int64(n)
			buf = append(buf, chunk[:n]...)
			if len(buf) > keep {
				buf = append(buf[:0], buf[len(buf)-keep:]...)
			}
		}
		if err == io.EOF {
			return buf, total, nil
		}
		if err != nil {
			return nil, total, err
		}
	}
}

// peekZip: старі журнали програма верстата пакує в .zip поруч. Без entry —
// перелік текстових файлів архіву; з entry — початок або кінець одного з
// них. Розпаковується лише в пам'ять і не більше zipMaxEntryBytes: на диск
// нічого не пишеться.
func peekZip(w http.ResponseWriter, f *os.File, path string, info os.FileInfo, entry string, tail bool) {
	archive, err := zip.NewReader(f, info.Size())
	if err != nil {
		http.Error(w, "архів не розібрався", http.StatusConflict)
		return
	}
	if entry == "" {
		items := []map[string]interface{}{}
		for _, item := range archive.File {
			if len(items) >= zipMaxEntries {
				break
			}
			if item.FileInfo().IsDir() {
				continue
			}
			items = append(items, map[string]interface{}{
				"name":  item.Name,
				"size":  int64(item.UncompressedSize64),
				"mtime": item.Modified.Format(time.RFC3339),
				"text":  textExt[strings.ToLower(filepath.Ext(item.Name))],
			})
		}
		writeJSON(w, map[string]interface{}{
			"path": path, "size": info.Size(), "mtime": info.ModTime().Format(time.RFC3339),
			"entries": items, "entries_total": len(archive.File),
		})
		return
	}
	for _, item := range archive.File {
		if item.Name != entry {
			continue
		}
		if !textExt[strings.ToLower(filepath.Ext(item.Name))] {
			http.Error(w, "у архіві не текстовий файл", http.StatusForbidden)
			return
		}
		rc, err := item.Open()
		if err != nil {
			http.Error(w, "файл в архіві не відкрився", http.StatusConflict)
			return
		}
		defer rc.Close()
		var data []byte
		if tail {
			data, _, err = lastBytes(rc, peekMaxBytes, zipMaxEntryBytes)
		} else {
			data, err = io.ReadAll(io.LimitReader(rc, peekMaxBytes))
		}
		if err != nil {
			http.Error(w, "файл в архіві не прочитався", http.StatusConflict)
			return
		}
		size := int64(item.UncompressedSize64)
		writeJSON(w, map[string]interface{}{
			"path": path, "entry": item.Name, "size": size,
			"mtime": item.Modified.Format(time.RFC3339), "truncated": size > int64(len(data)),
			"tail": tail, "data": base64.StdEncoding.EncodeToString(data),
		})
		return
	}
	http.Error(w, "такого файлу в архіві немає", http.StatusNotFound)
}
