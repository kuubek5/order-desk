//go:build !windows

// Заглушка, щоб пакет збирався на Linux-хості CI (`go mod tidy`, крос-збірка).
// Агент працює лише на Windows.

package main

import "os"

func captureStderr(*os.File) {}
