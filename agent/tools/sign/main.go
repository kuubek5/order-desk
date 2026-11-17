// sign — підписує зібраний kmill-agent.exe для самооновлення (internal/upd).
//
// Запускає лише CI: приватний ключ приходить зі змінної середовища
// AGENT_SIGNING_KEY (секрет GitHub, base64 від 32 байтів) і ніде не
// зберігається. Поруч з exe з'являються manifest.json і manifest.sig — саме
// їх CRM надсилає агентові разом із файлом.
//
//	sign -exe kmill-agent.exe -version 2026.11.17-abc1234 -built 1763400000
//
// Після підпису інструмент сам перевіряє результат ключами, зашитими в агента
// (upd.TrustedKeys): якщо секрет і зашитий ключ розійшлись, збірка падає тут,
// а не в цеху, де агент мовчки відмовився б від кожного оновлення.
package main

import (
	"encoding/base64"
	"encoding/hex"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"kmill-agent/internal/upd"
)

func main() {
	exe := flag.String("exe", "", "path to the built kmill-agent.exe")
	version := flag.String("version", "", "version string stamped into the exe")
	built := flag.Int64("built", 0, "build time, unix seconds (same value as main.BuildStamp)")
	flag.Parse()
	if err := run(*exe, *version, *built, os.Getenv("AGENT_SIGNING_KEY")); err != nil {
		fmt.Fprintln(os.Stderr, "sign:", err)
		os.Exit(1)
	}
}

func run(exe, version string, built int64, keyB64 string) error {
	if exe == "" || version == "" || built <= 0 {
		return fmt.Errorf("потрібні -exe, -version і -built")
	}
	seed, err := base64.StdEncoding.DecodeString(strings.TrimSpace(keyB64))
	if err != nil || len(seed) == 0 {
		return fmt.Errorf("AGENT_SIGNING_KEY порожній або не base64")
	}
	sum, size, err := upd.HashFile(exe)
	if err != nil {
		return err
	}
	raw, sig, err := upd.Sign(seed, upd.Manifest{Version: version, Built: built, SHA256: sum, Size: size})
	if err != nil {
		return err
	}
	keys, err := upd.ParseKeys(upd.TrustedKeys)
	if err != nil {
		return err
	}
	if _, err := upd.VerifyManifest(raw, sig, keys); err != nil {
		return fmt.Errorf("підпис не проходить перевірку ключами агента (секрет і upd.TrustedKeys розійшлись): %v", err)
	}
	dir := filepath.Dir(exe)
	if err := os.WriteFile(filepath.Join(dir, upd.ManifestName), raw, 0644); err != nil {
		return err
	}
	if err := os.WriteFile(filepath.Join(dir, upd.SigName), []byte(hex.EncodeToString(sig)), 0644); err != nil {
		return err
	}
	fmt.Printf("signed %s: version %s, built %d, sha256 %s, %d bytes\n", filepath.Base(exe), version, built, sum, size)
	return nil
}
