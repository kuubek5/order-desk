//go:build !windows

// Заглушка, щоб пакет збирався на Linux-хості CI під час `go mod tidy`.
// Агент працює лише на Windows.

package main

func visibleWindowTitles() []string { return nil }
