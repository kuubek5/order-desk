//go:build windows

package main

import (
	"syscall"
	"unsafe"
)

// Читання ЗАГОЛОВКІВ вікон на ПК верстата. Заголовок RemiCORE несе повне ім'я
// програми, напр. `Remote - zr18_18-Monolith-A3-x62_2026-09-02_23-04-33.iso`, а
// в ньому — дата+час, тобто той самий ідентифікатор, який оператор вписує в CRM
// як Sum3D ID. Беремо його ТЕКСТОМ з Windows, а не OCR-ом з картинки: тут
// здогадкам не місце — або точне ім'я, або нічого.
//
// Тільки читання: EnumWindows/GetWindowText нічого у верстат не шлють.

var (
	user32              = syscall.NewLazyDLL("user32.dll")
	procEnumWindows     = user32.NewProc("EnumWindows")
	procGetWindowTextW  = user32.NewProc("GetWindowTextW")
	procGetWindowTextLn = user32.NewProc("GetWindowTextLengthW")
	procIsWindowVisible = user32.NewProc("IsWindowVisible")
)

// visibleWindowTitles returns the titles of all visible top-level windows.
// Empty titles are skipped; the CRM picks the one it recognises.
func visibleWindowTitles() []string {
	var out []string
	cb := syscall.NewCallback(func(hwnd uintptr, _ uintptr) uintptr {
		visible, _, _ := procIsWindowVisible.Call(hwnd)
		if visible == 0 {
			return 1 // continue
		}
		n, _, _ := procGetWindowTextLn.Call(hwnd)
		if n == 0 {
			return 1
		}
		buf := make([]uint16, n+1)
		procGetWindowTextW.Call(hwnd, uintptr(unsafe.Pointer(&buf[0])), n+1)
		if title := syscall.UTF16ToString(buf); title != "" {
			out = append(out, title)
		}
		return 1
	})
	procEnumWindows.Call(cb, 0)
	return out
}
