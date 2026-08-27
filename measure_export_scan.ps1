# Виміряти, чи паралельний обхід теки export справді швидший за послідовний.
#
# Питання: 80 секунд на екрані видачі — це ЗАТРИМКА кожної ходки по SMB
# (тоді потоки дадуть виграш у рази) чи ПРОПУСКНА ЗДАТНІСТЬ сховища
# (тоді потоки майже нічого не змінять)? Без цієї цифри вибір між ними —
# ставка, а не рішення.
#
# Нічого не потребує: ні Python, ні репозиторію, ні ключів. Тільки ЧИТАЄ
# теки — не створює, не змінює, не видаляє.
#
# Запуск:
#   powershell -ExecutionPolicy Bypass -File measure_export_scan.ps1 "<шлях до export>"
#
# Шлях видно в Налаштуваннях → «Шлях до папки export».

param(
    [Parameter(Mandatory = $true)][string]$Root,
    [int]$Sample = 20,       # клієнтів на кожен з двох замірів
    [int]$Workers = 16,
    [int]$WindowDays = 37    # вікно видачі: 30 днів + тиждень запасу
)

$ErrorActionPreference = 'Stop'

# Обхід написаний на C#, а не на PowerShell, з двох причин: PowerShell 5.1
# не має ForEach-Object -Parallel, а DirectoryInfo.GetDirectories() віддає
# час створення РАЗОМ зі списком теки — так само, як os.scandir у самому
# застосунку. Інакше замір рахував би зайві ходки, яких у проді немає.
Add-Type -Language CSharp -TypeDefinition @'
using System;
using System.Diagnostics;
using System.IO;
using System.Threading.Tasks;

public static class ExportProbe
{
    public static int ScanClient(string root, string client, DateTime notBefore)
    {
        int entries = 0;
        DirectoryInfo[] batches;
        try { batches = new DirectoryInfo(Path.Combine(root, client)).GetDirectories(); }
        catch { return 0; }

        foreach (DirectoryInfo batch in batches)
        {
            // Стару партію пропускаємо, НЕ заходячи всередину — той самий
            // відсів, що робить застосунок.
            DateTime created;
            try { created = batch.CreationTime; } catch { continue; }
            if (created < notBefore) continue;

            DirectoryInfo[] materials;
            try { materials = batch.GetDirectories(); } catch { continue; }

            foreach (DirectoryInfo material in materials)
            {
                try { material.GetFiles(); } catch { continue; }
                entries++;
            }
        }
        return entries;
    }

    // Повертає { мілісекунди, знайдено записів }.
    public static long[] Sequential(string root, string[] clients, DateTime notBefore)
    {
        Stopwatch sw = Stopwatch.StartNew();
        long total = 0;
        foreach (string client in clients) total += ScanClient(root, client, notBefore);
        sw.Stop();
        return new long[] { sw.ElapsedMilliseconds, total };
    }

    public static long[] Parallel(string root, string[] clients, DateTime notBefore, int workers)
    {
        Stopwatch sw = Stopwatch.StartNew();
        long total = 0;
        object gate = new object();
        ParallelOptions options = new ParallelOptions();
        options.MaxDegreeOfParallelism = workers;
        System.Threading.Tasks.Parallel.ForEach(clients, options, delegate(string client)
        {
            int found = ScanClient(root, client, notBefore);
            lock (gate) { total += found; }
        });
        sw.Stop();
        return new long[] { sw.ElapsedMilliseconds, total };
    }
}
'@

if (-not (Test-Path -LiteralPath $Root)) {
    Write-Host "Немає такого шляху: $Root"
    exit 1
}

$notBefore = (Get-Date).AddDays(-$WindowDays)

$sw = [System.Diagnostics.Stopwatch]::StartNew()
$names = @([System.IO.Directory]::GetDirectories($Root) | ForEach-Object { Split-Path $_ -Leaf } | Sort-Object)
$sw.Stop()
Write-Host ("level-1 scandir: {0} client folders, {1:N2}s" -f $names.Count, ($sw.ElapsedMilliseconds / 1000))

if ($names.Count -lt ($Sample * 2)) {
    Write-Host ("need at least {0} client folders, got {1}" -f ($Sample * 2), $names.Count)
    exit 1
}

# Дві НЕПЕРЕСІЧНІ вибірки з різних кінців списку: після першого проходу SMB
# тримає теку в кеші, і повтор по тих самих клієнтах збрехав би на користь
# того заміру, що йде другим.
$seqNames = [string[]]($names[0..($Sample - 1)])
$parNames = [string[]]($names[($names.Count - $Sample)..($names.Count - 1)])

$seq = [ExportProbe]::Sequential($Root, $seqNames, $notBefore)
$par = [ExportProbe]::Parallel($Root, $parNames, $notBefore, $Workers)

$seqSeconds = $seq[0] / 1000
$parSeconds = $par[0] / 1000

Write-Host ("sequential:    {0} clients, {1} entries, {2:N2}s" -f $Sample, $seq[1], $seqSeconds)
Write-Host ("parallel x{0}:  {1} clients, {2} entries, {3:N2}s" -f $Workers, $Sample, $par[1], $parSeconds)

if ($parSeconds -gt 0) {
    Write-Host ("speedup: {0:N1}x" -f ($seqSeconds / $parSeconds))
}
Write-Host ("projected for 262 clients: sequential {0:N0}s, parallel {1:N0}s" -f `
    ($seqSeconds / $Sample * 262), ($parSeconds / $Sample * 262))
