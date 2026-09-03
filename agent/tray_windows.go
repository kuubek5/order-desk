//go:build windows

package main

// Іконка в треї для агента верстата.
//
// Навіщо: агент працює без вікна (збірка -H=windowsgui), і донедавна єдиним
// способом його зупинити був Диспетчер завдань. Оператор мусить бачити, що
// агент живий, і мати змогу вимкнути спостереження, не викликаючи адміна —
// це ПК верстата, і людина за ним має контроль над тим, що з нього віддається.
//
// Меню: «Налаштування» (відкриває сторінку на 8766), «Призупинити/Відновити»
// (кадр перестає віддаватись, CRM чесно бачить «немає зв'язку»), «Вийти».
//
// Реалізація — чистий syscall до user32/shell32: жодних сторонніх пакетів,
// тож крос-компіляція з Linux-CI (CGO_ENABLED=0) лишається такою ж простою.

import (
	"log"
	"syscall"
	"unsafe"
)

var (
	shell32              = syscall.NewLazyDLL("shell32.dll")
	procShellNotifyIcon  = shell32.NewProc("Shell_NotifyIconW")
	procExtractIconEx    = shell32.NewProc("ExtractIconExW")
	kernel32             = syscall.NewLazyDLL("kernel32.dll")
	procGetModuleHandleW = kernel32.NewProc("GetModuleHandleW")

	procRegisterClassExW   = user32.NewProc("RegisterClassExW")
	procCreateWindowExW    = user32.NewProc("CreateWindowExW")
	procDefWindowProcW     = user32.NewProc("DefWindowProcW")
	procGetMessageW        = user32.NewProc("GetMessageW")
	procTranslateMessage   = user32.NewProc("TranslateMessage")
	procDispatchMessageW   = user32.NewProc("DispatchMessageW")
	procPostQuitMessage    = user32.NewProc("PostQuitMessage")
	procCreatePopupMenu    = user32.NewProc("CreatePopupMenu")
	procAppendMenuW        = user32.NewProc("AppendMenuW")
	procDestroyMenu        = user32.NewProc("DestroyMenu")
	procTrackPopupMenu     = user32.NewProc("TrackPopupMenu")
	procGetCursorPos       = user32.NewProc("GetCursorPos")
	procSetForegroundWindow = user32.NewProc("SetForegroundWindow")
	procLoadIconW          = user32.NewProc("LoadIconW")
	procPostMessageW       = user32.NewProc("PostMessageW")
)

const (
	wmDestroy     = 0x0002
	wmCommand     = 0x0111
	wmTrayCallback = 0x0400 + 1 // WM_APP+1
	wmRButtonUp   = 0x0205
	wmLButtonDown = 0x0201

	nimAdd    = 0x0
	nimModify = 0x1
	nimDelete = 0x2

	nifMessage = 0x1
	nifIcon    = 0x2
	nifTip     = 0x4

	mfString    = 0x0
	mfSeparator = 0x800

	tpmLeftAlign  = 0x0
	tpmRightButton = 0x2

	idOpen  = 1001
	idPause = 1002
	idQuit  = 1003
)

type notifyIconData struct {
	CbSize           uint32
	HWnd             syscall.Handle
	UID              uint32
	UFlags           uint32
	UCallbackMessage uint32
	HIcon            syscall.Handle
	SzTip            [128]uint16
	// Далі структура має ще поля, але з cbSize під цю частину Windows
	// заповнює решту нулями — нам більшого не треба.
}

type point struct{ X, Y int32 }

type msg struct {
	HWnd    syscall.Handle
	Message uint32
	WParam  uintptr
	LParam  uintptr
	Time    uint32
	Pt      point
}

type wndClassEx struct {
	CbSize        uint32
	Style         uint32
	LpfnWndProc   uintptr
	CbClsExtra    int32
	CbWndExtra    int32
	HInstance     syscall.Handle
	HIcon         syscall.Handle
	HCursor       syscall.Handle
	HbrBackground syscall.Handle
	LpszMenuName  *uint16
	LpszClassName *uint16
	HIconSm       syscall.Handle
}

func utf16(s string) *uint16 {
	p, _ := syscall.UTF16PtrFromString(s)
	return p
}

// appIcon бере іконку з САМОГО exe (її кладе туди інсталятор/лінкер). Якщо не
// вийшло — стандартна системна, аби трей усе одно з'явився.
func appIcon(hInst syscall.Handle) syscall.Handle {
	exe, err := syscall.UTF16PtrFromString(exePath())
	if err == nil {
		var large, small syscall.Handle
		r, _, _ := procExtractIconEx.Call(
			uintptr(unsafe.Pointer(exe)), 0,
			uintptr(unsafe.Pointer(&large)), uintptr(unsafe.Pointer(&small)), 1,
		)
		if r > 0 && small != 0 {
			return small
		}
		if r > 0 && large != 0 {
			return large
		}
	}
	h, _, _ := procLoadIconW.Call(0, 32512) // IDI_APPLICATION
	return syscall.Handle(h)
}

var trayHWnd syscall.Handle

// runTray тримає власний цикл повідомлень. Кличеться з окремої горутини —
// HTTP-сервер агента живе паралельно й від трею не залежить (вимкнули трей —
// кадр усе одно віддається, поки процес живий).
func runTray(onQuit func()) {
	hInst, _, _ := procGetModuleHandleW.Call(0)
	className := utf16("KMillAgentTray")

	wndProc := syscall.NewCallback(func(hwnd syscall.Handle, message uint32, wparam, lparam uintptr) uintptr {
		switch message {
		case wmTrayCallback:
			// Правий клік або лівий — обидва відкривають меню: на дрібній
			// іконці «правильна» кнопка не очевидна, а помилка коштує пошуку.
			if lparam == wmRButtonUp || lparam == wmLButtonDown {
				showTrayMenu(hwnd)
			}
		case wmCommand:
			switch wparam & 0xffff {
			case idOpen:
				openBrowser("http://" + setupAddr + "/")
			case idPause:
				togglePaused()
			case idQuit:
				removeTrayIcon(hwnd)
				if onQuit != nil {
					onQuit()
				}
				procPostQuitMessage.Call(0)
			}
		case wmDestroy:
			removeTrayIcon(hwnd)
			procPostQuitMessage.Call(0)
		}
		r, _, _ := procDefWindowProcW.Call(uintptr(hwnd), uintptr(message), wparam, lparam)
		return r
	})

	wc := wndClassEx{
		CbSize:        uint32(unsafe.Sizeof(wndClassEx{})),
		LpfnWndProc:   wndProc,
		HInstance:     syscall.Handle(hInst),
		LpszClassName: className,
	}
	procRegisterClassExW.Call(uintptr(unsafe.Pointer(&wc)))

	hwnd, _, _ := procCreateWindowExW.Call(
		0, uintptr(unsafe.Pointer(className)), uintptr(unsafe.Pointer(utf16("KMill Agent"))),
		0, 0, 0, 0, 0, 0, 0, hInst, 0,
	)
	if hwnd == 0 {
		log.Printf("tray: вікно не створено — працюємо без іконки")
		return
	}
	trayHWnd = syscall.Handle(hwnd)
	addTrayIcon(trayHWnd, syscall.Handle(hInst))

	var m msg
	for {
		r, _, _ := procGetMessageW.Call(uintptr(unsafe.Pointer(&m)), 0, 0, 0)
		if int32(r) <= 0 {
			return
		}
		procTranslateMessage.Call(uintptr(unsafe.Pointer(&m)))
		procDispatchMessageW.Call(uintptr(unsafe.Pointer(&m)))
	}
}

func newNotifyData(hwnd syscall.Handle) notifyIconData {
	nid := notifyIconData{
		CbSize:           uint32(unsafe.Sizeof(notifyIconData{})),
		HWnd:             hwnd,
		UID:              1,
		UFlags:           nifMessage | nifIcon | nifTip,
		UCallbackMessage: wmTrayCallback,
	}
	return nid
}

func trayTip() string {
	if isPaused() {
		return "KMill Agent — призупинено"
	}
	return "KMill Agent — спостереження активне"
}

func addTrayIcon(hwnd, hInst syscall.Handle) {
	nid := newNotifyData(hwnd)
	nid.HIcon = appIcon(hInst)
	copy(nid.SzTip[:], syscall.StringToUTF16(trayTip()))
	procShellNotifyIcon.Call(nimAdd, uintptr(unsafe.Pointer(&nid)))
}

func updateTrayIcon() {
	if trayHWnd == 0 {
		return
	}
	nid := newNotifyData(trayHWnd)
	nid.HIcon = appIcon(0)
	copy(nid.SzTip[:], syscall.StringToUTF16(trayTip()))
	procShellNotifyIcon.Call(nimModify, uintptr(unsafe.Pointer(&nid)))
}

func removeTrayIcon(hwnd syscall.Handle) {
	nid := newNotifyData(hwnd)
	procShellNotifyIcon.Call(nimDelete, uintptr(unsafe.Pointer(&nid)))
}

func showTrayMenu(hwnd syscall.Handle) {
	hMenu, _, _ := procCreatePopupMenu.Call()
	if hMenu == 0 {
		return
	}
	defer procDestroyMenu.Call(hMenu)

	pauseLabel := "Призупинити спостереження"
	if isPaused() {
		pauseLabel = "Відновити спостереження"
	}
	procAppendMenuW.Call(hMenu, mfString, idOpen, uintptr(unsafe.Pointer(utf16("Налаштування агента"))))
	procAppendMenuW.Call(hMenu, mfString, idPause, uintptr(unsafe.Pointer(utf16(pauseLabel))))
	procAppendMenuW.Call(hMenu, mfSeparator, 0, 0)
	procAppendMenuW.Call(hMenu, mfString, idQuit, uintptr(unsafe.Pointer(utf16("Вийти"))))

	var pt point
	procGetCursorPos.Call(uintptr(unsafe.Pointer(&pt)))
	// Без SetForegroundWindow меню не закривається кліком повз нього — відома
	// поведінка Windows для трей-меню.
	procSetForegroundWindow.Call(uintptr(hwnd))
	procTrackPopupMenu.Call(
		hMenu, tpmLeftAlign|tpmRightButton,
		uintptr(pt.X), uintptr(pt.Y), 0, uintptr(hwnd), 0,
	)
	procPostMessageW.Call(uintptr(hwnd), 0, 0, 0)
}
