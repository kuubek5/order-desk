//go:build !windows

// Stubs so the package still compiles on the Linux CI host during `go mod
// tidy`. The agent only ever runs on Windows.

package main

func isAdmin() bool { return true }

func relaunchElevated(args ...string) error { return nil }
