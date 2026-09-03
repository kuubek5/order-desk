//go:build !windows

// Заглушки, щоб пакет збирався на Linux-хості CI під час `go mod tidy`.
// Агент працює лише на Windows.

package main

func runTray(onQuit func()) {}

func updateTrayIcon() {}
