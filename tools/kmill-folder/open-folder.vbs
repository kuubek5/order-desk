' Обробник протоколу kmill-folder:// — відкриває мережеву теку в Провіднику на
' ПК ОПЕРАТОРА. Застосунок KuubMill працює на сервері й не може відкрити
' Провідник на чужому ПК; для мережевих клієнтів він віддає посилання
' kmill-folder://<base64url шляху>, а цей помічник (зареєстрований у реєстрі
' цього ПК) декодує шлях і відкриває теку локально.
'
' Аргумент від Windows — увесь URL як %1: "kmill-folder://<b64url>/". Шлях
' закодовано base64url (без службових символів UNC у самому URL); тут його
' декодуємо назад у UTF-8 і передаємо explorer.exe. Помилки ковтаємо тихо —
' помічник не має нічого показувати, лише відкривати теку.
Option Explicit

Dim arg
If WScript.Arguments.Count = 0 Then WScript.Quit
arg = WScript.Arguments(0)

' Зняти схему й будь-які слеші-роздільники, які додає браузер/ОС.
arg = Replace(arg, "kmill-folder://", "")
arg = Replace(arg, "kmill-folder:", "")
arg = Replace(arg, "/", "")
arg = Replace(arg, "\", "")
arg = Trim(arg)
If Len(arg) = 0 Then WScript.Quit

' base64url -> base64 + паддинг.
arg = Replace(arg, "-", "+")
arg = Replace(arg, "_", "/")
Do While (Len(arg) Mod 4) <> 0
  arg = arg & "="
Loop

Dim path
On Error Resume Next
path = B64ToUtf8(arg)
On Error Goto 0
If Len(path) = 0 Then WScript.Quit

' Відкрити теку. Лапки — на випадок пробілів у шляху (UNC із іменем клієнта).
CreateObject("WScript.Shell").Run "explorer.exe """ & path & """", 1, False

Function B64ToUtf8(s)
  Dim node, stream
  Set node = CreateObject("MSXML2.DOMDocument.6.0").createElement("b64")
  node.dataType = "bin.base64"
  node.text = s
  Set stream = CreateObject("ADODB.Stream")
  stream.Type = 1            ' бінарний
  stream.Open
  stream.Write node.nodeTypedValue
  stream.Position = 0
  stream.Type = 2            ' текст
  stream.Charset = "utf-8"
  B64ToUtf8 = stream.ReadText
  stream.Close
End Function
