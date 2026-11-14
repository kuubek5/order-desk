//go:build !windows

// Заглушки, щоб пакет і тести збирались на Linux-хості CI. Агент працює
// лише на Windows.

package main

import "os"

func fixedDrives() []string { return nil }

func listDir(path string) ([]dirEntry, error) {
	entries, err := os.ReadDir(path)
	if err != nil {
		return nil, err
	}
	var out []dirEntry
	for _, e := range entries {
		info, err := e.Info()
		if err != nil {
			continue
		}
		out = append(out, dirEntry{
			Name:  e.Name(),
			IsDir: e.IsDir(),
			Skip:  info.Mode()&os.ModeSymlink != 0,
			Size:  info.Size(),
			MTime: info.ModTime(),
		})
	}
	return out, nil
}

func openShared(path string) (*os.File, error) { return os.Open(path) }
