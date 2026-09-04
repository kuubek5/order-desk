//go:build windows

package main

import (
	"os"

	"golang.org/x/sys/windows"
)

// captureStderr спрямовує stderr процесу у файл логу.
//
// Навіщо: релізний exe зібраний із -H=windowsgui, тобто БЕЗ консолі. Пакет
// `log` ми вже дублюємо у файл, але аварійне завершення Go (паніка, фатальна
// помилка рантайму) пише не через `log`, а напряму в stderr — а він у
// windowsgui-процесі веде в нікуди. Тому досі кожен крах агента був
// безслідним: у цеху лишався тільки «привид» іконки в треї, який зникає при
// наведенні мишкою, і мертвий верстат у CRM без жодного пояснення (бойовий
// випадок 04.09.26).
//
// SetStdHandle потрібен на додачу до os.Stderr: рантайм Go бере дескриптор
// саме через нього, а не через змінну пакета os.
func captureStderr(f *os.File) {
	if f == nil {
		return
	}
	_ = windows.SetStdHandle(windows.STD_ERROR_HANDLE, windows.Handle(f.Fd()))
	os.Stderr = f
}
