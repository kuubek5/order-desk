package main

import (
	"archive/zip"
	"encoding/base64"
	"encoding/json"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func mustWrite(t *testing.T, path, text string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(text), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestAllowedRootsTakeOnlyMachineFoldersAndNeverOurOwn(t *testing.T) {
	top := t.TempDir()
	mustWrite(t, filepath.Join(top, "RemiCORE", "ToolProperties.INI"), "ActRunTime=1")
	mustWrite(t, filepath.Join(top, "Photos", "a.txt"), "x")
	mustWrite(t, filepath.Join(top, "KMill Agent", "agent.json"), "{}")
	mustWrite(t, filepath.Join(top, "mill-own", "agent.json"), "{}")
	roots := allowedRoots([]string{top}, listDir, filepath.Join(top, "mill-own"))
	if len(roots) != 1 || filepath.Base(roots[0]) != "RemiCORE" {
		t.Fatalf("roots = %v", roots)
	}
}

func TestWalkListsFilesWithSizeAndStopsAtLimits(t *testing.T) {
	top := t.TempDir()
	root := filepath.Join(top, "imes-icore")
	mustWrite(t, filepath.Join(root, "cfg", "Toolstat.INI"), "12345")
	mustWrite(t, filepath.Join(root, "Spindle.LOG"), "x")
	files, dirs, truncated := walkRoots([]string{root}, listDir, time.Now().Add(time.Minute), "")
	if len(files) != 2 || dirs != 2 || truncated {
		t.Fatalf("files=%v dirs=%d truncated=%v", files, dirs, truncated)
	}
	for _, f := range files {
		if filepath.Base(f.Path) == "Toolstat.INI" && f.Size != 5 {
			t.Fatalf("size = %d", f.Size)
		}
	}
	_, _, late := walkRoots([]string{root}, listDir, time.Now().Add(-time.Second), "")
	if !late {
		t.Fatal("минулий дедлайн мусить обірвати обхід")
	}
}

func TestWithinRejectsOutsideAndRelativePaths(t *testing.T) {
	top := t.TempDir()
	root := filepath.Join(top, "RemiCORE")
	if !within(filepath.Join(root, "a", "b.ini"), []string{root}) {
		t.Fatal("файл у теці мусить проходити")
	}
	if within(filepath.Join(top, "RemiCORE-other", "b.ini"), []string{root}) {
		t.Fatal("сусідня тека з тим самим початком назви — не всередині")
	}
	if within(filepath.Join(root, "..", "secret.ini"), []string{root}) {
		t.Fatal("вихід через .. мусить відсікатись")
	}
	if within("relative.ini", []string{root}) {
		t.Fatal("відносний шлях не приймається")
	}
}

func TestTopNamesGiveFolderNamesOnly(t *testing.T) {
	top := t.TempDir()
	mustWrite(t, filepath.Join(top, "Zeta", "deep", "f.txt"), "x")
	mustWrite(t, filepath.Join(top, "file.txt"), "x")
	names := topNames([]string{top}, listDir)[top]
	if len(names) != 1 || names[0] != "Zeta" {
		t.Fatalf("names = %v", names)
	}
}

func TestReadPartGivesHeadOrTail(t *testing.T) {
	text := strings.Repeat("a", peekMaxBytes) + "FRESH"
	head, offset, err := readPart(strings.NewReader(text), int64(len(text)), false)
	if err != nil || offset != 0 || len(head) != peekMaxBytes || strings.Contains(string(head), "FRESH") {
		t.Fatalf("head: offset=%d len=%d err=%v", offset, len(head), err)
	}
	tail, offset, err := readPart(strings.NewReader(text), int64(len(text)), true)
	if err != nil || offset != 5 || !strings.HasSuffix(string(tail), "FRESH") {
		t.Fatalf("tail: offset=%d len=%d err=%v", offset, len(tail), err)
	}
	small, offset, _ := readPart(strings.NewReader("short"), 5, true)
	if string(small) != "short" || offset != 0 {
		t.Fatalf("small tail: %q %d", small, offset)
	}
}

func TestLastBytesKeepsTheEndOfAStream(t *testing.T) {
	text := strings.Repeat("x", 100000) + "END"
	got, total, err := lastBytes(strings.NewReader(text), 10, 1<<20)
	if err != nil || string(got) != "xxxxxxxEND" || total != int64(len(text)) {
		t.Fatalf("got %q total=%d err=%v", got, total, err)
	}
}

func TestPeekZipListsAndReadsTextEntriesOnly(t *testing.T) {
	path := filepath.Join(t.TempDir(), "logs.zip")
	out, err := os.Create(path)
	if err != nil {
		t.Fatal(err)
	}
	zw := zip.NewWriter(out)
	for name, body := range map[string]string{"Server.log": "line1\nline2\nlast", "data.bin": "\x00\x01"} {
		entry, _ := zw.Create(name)
		entry.Write([]byte(body))
	}
	zw.Close()
	out.Close()

	call := func(entry string, tail bool) (*httptest.ResponseRecorder, map[string]interface{}) {
		f, err := os.Open(path)
		if err != nil {
			t.Fatal(err)
		}
		defer f.Close()
		info, _ := f.Stat()
		rec := httptest.NewRecorder()
		peekZip(rec, f, path, info, entry, tail)
		var body map[string]interface{}
		json.Unmarshal(rec.Body.Bytes(), &body)
		return rec, body
	}
	rec, body := call("", false)
	if rec.Code != 200 || len(body["entries"].([]interface{})) != 2 {
		t.Fatalf("list: %d %s", rec.Code, rec.Body.String())
	}
	rec, body = call("Server.log", true)
	raw, _ := base64.StdEncoding.DecodeString(body["data"].(string))
	if rec.Code != 200 || string(raw) != "line1\nline2\nlast" || body["tail"] != true {
		t.Fatalf("entry: %d %s", rec.Code, rec.Body.String())
	}
	if rec, _ = call("data.bin", false); rec.Code != 403 {
		t.Fatalf("binary entry must be refused, got %d", rec.Code)
	}
	if rec, _ = call("missing.log", false); rec.Code != 404 {
		t.Fatalf("missing entry: %d", rec.Code)
	}
}
