package main

import (
	"os"
	"path/filepath"
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
