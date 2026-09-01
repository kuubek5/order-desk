//go:build windows

package main

import (
	"syscall"

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
