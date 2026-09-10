//go:build windows

package main

import (
	"sync"
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

// ОДИН колбек на весь процес — не на кожен запит.
//
// `syscall.NewCallback` займає комірку з запасу рантайму Go (~2000 на процес),
// і звільнити її неможливо. Досі колбек-замикання створювалось у кожному
// виклику visibleWindowTitles, а CRM питає /titles на кожному вдалому тіку —
// раз на 5 с. Запас закінчувався за ~2 год 47 хв, і процес падав з
// `fatal error: too many callback functions`, яку не перехоплює жоден recover.
// Далі порт мовчав, доки сторожова задача не піднімала агента на найближчій
// п'ятихвилинній межі. Саме це в цеху виглядало як «моргнула мережа — віджети
// на кілька хвилин втратили верстат» (150i і 250i, 10.09.26: обидва обриви
// скінчились через ~8 с після 08:55:00 і 09:00:00).
//
// Колбек — функція верхнього рівня, а результат збирається в спільний зріз під
// м'ютексом: запити /titles обробляються паралельно, а EnumWindows кличе
// колбек синхронно, у тому самому потоці, тож м'ютекс тримається рівно на час
// одного обходу вікон.
var (
	titlesMu  sync.Mutex
	titlesOut []string
	titlesCb  = syscall.NewCallback(collectVisibleTitle)
)

func collectVisibleTitle(hwnd uintptr, _ uintptr) uintptr {
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
		titlesOut = append(titlesOut, title)
	}
	return 1
}

// visibleWindowTitles returns the titles of all visible top-level windows.
// Empty titles are skipped; the CRM picks the one it recognises.
func visibleWindowTitles() []string {
	titlesMu.Lock()
	defer titlesMu.Unlock()
	titlesOut = nil
	procEnumWindows.Call(titlesCb, 0)
	out := titlesOut
	titlesOut = nil
	return out
}
