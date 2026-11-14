//go:build windows

package main

import (
	"os"
	"strings"
	"syscall"
	"time"
	"unsafe"
)

const (
	attrDirectory    = 0x10
	attrReparsePoint = 0x400
	driveFixed       = 3
)

var procGetDriveTypeW = kernel32.NewProc("GetDriveTypeW")

// fixedDrives: лише локальні диски. Мережевий диск, що відпав, вішає
// звернення на десятки секунд — його не чіпаємо взагалі.
func fixedDrives() []string {
	var out []string
	for letter := 'C'; letter <= 'Z'; letter++ {
		root := string(letter) + `:\`
		ptr, err := syscall.UTF16PtrFromString(root)
		if err != nil {
			continue
		}
		kind, _, _ := procGetDriveTypeW.Call(uintptr(unsafe.Pointer(ptr)))
		if kind == driveFixed {
			out = append(out, root)
		}
	}
	return out
}

// listDir читає теку через FindFirstFile: розмір і час зміни приходять разом
// з назвою, окремого звернення до кожного файлу немає, і тека не замикається.
func listDir(path string) ([]dirEntry, error) {
	pattern, err := syscall.UTF16PtrFromString(strings.TrimRight(path, `\/`) + `\*`)
	if err != nil {
		return nil, err
	}
	var data syscall.Win32finddata
	handle, err := syscall.FindFirstFile(pattern, &data)
	if err != nil {
		return nil, err
	}
	defer syscall.FindClose(handle)
	var out []dirEntry
	for {
		name := syscall.UTF16ToString(data.FileName[:])
		if name != "." && name != ".." {
			out = append(out, dirEntry{
				Name:  name,
				IsDir: data.FileAttributes&attrDirectory != 0,
				Skip:  data.FileAttributes&attrReparsePoint != 0,
				Size:  int64(data.FileSizeHigh)<<32 | int64(data.FileSizeLow),
				MTime: time.Unix(0, data.LastWriteTime.Nanoseconds()),
			})
		}
		if err := syscall.FindNextFile(handle, &data); err != nil {
			break
		}
	}
	return out, nil
}

// openShared відкриває файл ЛИШЕ на читання й дозволяє іншим писати,
// перейменовувати й видаляти його. Звичайний os.Open видалення не дозволяє:
// програма верстата не змогла б прибрати свій файл, поки ми його читаємо.
func openShared(path string) (*os.File, error) {
	ptr, err := syscall.UTF16PtrFromString(path)
	if err != nil {
		return nil, err
	}
	handle, err := syscall.CreateFile(
		ptr, syscall.GENERIC_READ,
		syscall.FILE_SHARE_READ|syscall.FILE_SHARE_WRITE|syscall.FILE_SHARE_DELETE,
		nil, syscall.OPEN_EXISTING, syscall.FILE_ATTRIBUTE_NORMAL, 0,
	)
	if err != nil {
		return nil, err
	}
	return os.NewFile(uintptr(handle), path), nil
}
