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
	path := filepath.Clean(r.URL.Query().Get("path"))
	own := exeDir()
	if !within(path, allowedRoots(parentDirs(), listDir, own)) || (own != "" && within(path, []string{own})) {
		http.Error(w, "поза теками програми верстата", http.StatusForbidden)
		return
	}
	if !textExt[strings.ToLower(filepath.Ext(path))] {
		http.Error(w, "не текстовий файл налаштувань чи журналу", http.StatusForbidden)
		return
	}
	info, err := os.Stat(path)
	if err != nil || info.IsDir() {
		http.Error(w, "файлу немає", http.StatusNotFound)
		return
	}
	if info.Size() > peekMaxFileBytes {
		http.Error(w, "файл завеликий", http.StatusRequestEntityTooLarge)
		return
	}
	f, err := openShared(path)
	if err != nil {
		http.Error(w, "не відкрився", http.StatusConflict)
		return
	}
	defer f.Close()
	data, err := io.ReadAll(io.LimitReader(f, peekMaxBytes))
	if err != nil {
		http.Error(w, "не прочитався", http.StatusConflict)
		return
	}
	writeJSON(w, map[string]interface{}{
		"path":      path,
		"size":      info.Size(),
		"mtime":     info.ModTime().Format(time.RFC3339),
		"truncated": info.Size() > int64(len(data)),
		"data":      base64.StdEncoding.EncodeToString(data),
	})
}
