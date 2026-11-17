//go:build !e2e

package upd

import "testing"

// У звичайній збірці довірений ключ рівно один — справжній. Тестовий ключ
// (keys_e2e.go) має відому приватну частину; якби він потрапив у реліз, кожен
// міг би підписати «оновлення» для верстатів.
func TestNoTestKeyInNormalBuild(t *testing.T) {
	if len(TrustedKeys) != 1 || TrustedKeys[0] != "20bd5af8a0e0adfe4b42f3fe0fb4cb64bb1293cbafd5d0f469ad7cc446fca344" {
		t.Fatalf("несподіваний список довірених ключів: %v", TrustedKeys)
	}
}
