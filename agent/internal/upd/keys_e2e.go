//go:build e2e

package upd

// Лише для наскрізного тесту (go test -tags e2e): додає тестовий ключ, чия
// приватна частина відома (32 байти 0x07). У звичайну збірку цей файл не
// потрапляє — тег e2e CI при збірці агента не ставить, і тест нижче це стереже
// (TestNoTestKeyInNormalBuild).
func init() {
	TrustedKeys = append(TrustedKeys, "ea4a6c63e29c520abef5507b132ec5f9954776aebebe7b92421eea691446d22c")
}
