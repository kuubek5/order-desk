//go:build windows

package main

import (
	"syscall"
	"unsafe"

	"golang.org/x/sys/windows"
)

// isAdmin reports whether the current process runs with Administrator rights.
// Registering the scheduled task and adding a firewall rule need them.
func isAdmin() bool {
	var sid *windows.SID
	err := windows.AllocateAndInitializeSid(
		&windows.SECURITY_NT_AUTHORITY, 2,
		windows.SECURITY_BUILTIN_DOMAIN_RID,
		windows.DOMAIN_ALIAS_RID_ADMINS,
		0, 0, 0, 0, 0, 0, &sid)
	if err != nil {
		return false
	}
	defer windows.FreeSid(sid)
	token := windows.GetCurrentProcessToken()
	member, err := token.IsMember(sid)
	return err == nil && member
}

// runsElevated каже, чи процес працює з ПІДВИЩЕНИМИ правами при ввімкненому
// UAC (повний токен адміністратора, отриманий через підвищення). Саме тоді
// запускати файл з теки користувача не можна: його могла б підмінити звичайна
// програма того самого користувача й отримати права адміністратора.
//
// Обліковий запис адміністратора з ВИМКНЕНИМ UAC сюди не потрапляє (тип токена
// «за замовчуванням»): там межі прав немає взагалі, будь-яка програма
// користувача вже адміністратор, і відмова від оновлення нічого не захистила б
// — лише назавжди вимкнула б самооновлення на такому ПК.
func runsElevated() bool {
	var kind uint32
	var n uint32
	err := windows.GetTokenInformation(
		windows.GetCurrentProcessToken(), windows.TokenElevationType,
		(*byte)(unsafe.Pointer(&kind)), uint32(unsafe.Sizeof(kind)), &n)
	if err != nil {
		// Не вдалось дізнатись — вважаємо підвищеним: краще не оновитись.
		return true
	}
	const tokenElevationTypeFull = 2
	return kind == tokenElevationTypeFull
}

// relaunchElevated re-runs this exe with the given args through a UAC prompt.
func relaunchElevated(args ...string) error {
	verb, _ := syscall.UTF16PtrFromString("runas")
	exe, err := windows.UTF16PtrFromString(exePath())
	if err != nil {
		return err
	}
	var argp *uint16
	if len(args) > 0 {
		argp, _ = syscall.UTF16PtrFromString(joinArgs(args))
	}
	return windows.ShellExecute(0, verb, exe, argp, nil, windows.SW_SHOWNORMAL)
}

func joinArgs(args []string) string {
	s := ""
	for i, a := range args {
		if i > 0 {
			s += " "
		}
		s += a
	}
	return s
}
