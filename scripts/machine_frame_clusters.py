"""Згрупувати кадри верстата за ВИГЛЯДОМ екрана і зібрати контактний лист.

Навіщо. Кадрів із верстата набираються сотні, і 95%% з них — той самий
екран із іншими цифрами. Дивитись їх поспіль безглуздо: цікаві рівно ті,
що НЕ схожі на звичайний робочий екран — помилка, діалог, кінець програми,
очікування старту. Скрипт відсіює однакове й лишає представників.

Як. Підпис кадру — сильно зменшена сіра мініатюра (SIGNATURE_SIZE). Цифри
прогресу й годинника на ній займають частку пікселя, тож «той самий екран
із іншим відсотком» дає майже однаковий підпис, а інша РОЗКЛАДКА (вікно
помилки, порожній екран, інший режим) — помітно інший. Далі жадібна
кластеризація за середньою різницею яскравості.

Свідомо БЕЗ порогів «на око»: поріг задається аргументом, а звіт друкує
відстані між кластерами, щоб було видно, чи він адекватний саме цим кадрам.

Використання:
    python scripts/machine_frame_clusters.py calibration_frames/192.168.1.90
    python scripts/machine_frame_clusters.py frames.zip --out report --threshold 6

Результат: report/clusters.html (контактний лист) + представники PNG.
"""

from __future__ import annotations

import argparse
import base64
import io
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from PIL import Image

# Мініатюра для підпису. 64×48 — компроміс: дрібніше зливає вікно помилки з
# робочим екраном, більше починає реагувати на цифри, які нас тут не цікавлять.
SIGNATURE_SIZE = (64, 48)
# Середня різниця яскравості (0..255), нижче якої кадри вважаємо тим самим
# виглядом. 6 підібрано як «цифри й годинник не розводять, вікно поверх —
# розводить»; перевіряється звітом, а не вірою.
DEFAULT_THRESHOLD = 6.0
THUMB_WIDTH = 460


@dataclass
class Cluster:
    signature: list[int]
    members: list[str] = field(default_factory=list)
    sample: Image.Image | None = None
    sample_name: str = ""


def signature_of(image: Image.Image) -> list[int]:
    # tobytes(), не getdata(): getdata() у Pillow 13 вже deprecated, а нам
    # потрібен саме плаский масив байтів яскравості.
    return list(image.convert("L").resize(SIGNATURE_SIZE, Image.BILINEAR).tobytes())


def distance(a: list[int], b: list[int]) -> float:
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def load_frames(source: Path) -> list[tuple[str, Image.Image]]:
    """Кадри з теки (рекурсивно) або прямо з zip — оператор шле саме zip."""
    frames: list[tuple[str, Image.Image]] = []
    if source.is_file() and source.suffix.lower() == ".zip":
        with zipfile.ZipFile(source) as archive:
            for name in sorted(archive.namelist()):
                if not name.lower().endswith(".png"):
                    continue
                with archive.open(name) as handle:
                    frames.append((name, Image.open(io.BytesIO(handle.read())).convert("RGB")))
        return frames
    for png in sorted(source.rglob("*.png")):
        frames.append((str(png.relative_to(source)), Image.open(png).convert("RGB")))
    return frames


def cluster_frames(frames: list[tuple[str, Image.Image]], threshold: float) -> list[Cluster]:
    clusters: list[Cluster] = []
    for name, image in frames:
        sig = signature_of(image)
        for cluster in clusters:
            if distance(sig, cluster.signature) <= threshold:
                cluster.members.append(name)
                break
        else:
            clusters.append(Cluster(signature=sig, members=[name], sample=image, sample_name=name))
    clusters.sort(key=lambda c: len(c.members), reverse=True)
    return clusters


def thumb_data_uri(image: Image.Image) -> str:
    thumb = image.copy()
    thumb.thumbnail((THUMB_WIDTH, THUMB_WIDTH), Image.LANCZOS)
    buffer = io.BytesIO()
    thumb.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def build_report(clusters: list[Cluster], total: int, threshold: float, source: str) -> str:
    rows = []
    for index, cluster in enumerate(clusters, start=1):
        share = len(cluster.members) * 100 // max(total, 1)
        # Відстань до найбільшого кластера = «наскільки цей вигляд незвичний».
        gap = distance(cluster.signature, clusters[0].signature) if clusters else 0.0
        badge = "звичайний робочий екран" if index == 1 else f"відрізняється на {gap:.1f}"
        examples = ", ".join(cluster.members[:4])
        rows.append(
            f"""<section class="cluster">
  <header>
    <h2>Вигляд {index}</h2>
    <p><b>{len(cluster.members)}</b> кадрів ({share}%) · {badge}</p>
    <p class="files">{examples}{" …" if len(cluster.members) > 4 else ""}</p>
    <p class="ask">Що це на верстаті? _______________________________</p>
  </header>
  <img src="{thumb_data_uri(cluster.sample)}" alt="Вигляд {index}">
</section>"""
        )
    stamp = datetime.now().strftime("%d.%m.%Y %H:%M")
    return f"""<!doctype html>
<html lang="uk"><meta charset="utf-8">
<title>Вигляди екрана верстата</title>
<style>
 body {{ font: 14px/1.5 system-ui, sans-serif; margin: 24px; background: #14110f; color: #efe7df; }}
 h1 {{ font-size: 22px; }}
 .meta {{ color: #a89b90; }}
 .cluster {{ border: 1px solid #3a322c; border-radius: 12px; padding: 16px; margin: 18px 0; }}
 .cluster h2 {{ margin: 0 0 4px; font-size: 17px; }}
 .cluster p {{ margin: 2px 0; }}
 .files {{ color: #a89b90; font-family: ui-monospace, monospace; font-size: 12px; }}
 .ask {{ color: #e8b48b; margin-top: 8px; }}
 img {{ max-width: 100%; border: 1px solid #3a322c; border-radius: 8px; margin-top: 10px; }}
</style>
<h1>Вигляди екрана верстата</h1>
<p class="meta">{source} · {total} кадрів · {len(clusters)} різних виглядів · поріг {threshold} · {stamp}</p>
<p class="meta">Перший вигляд — найчастіший (як правило, звичайна робота). Далі — те,
що на нього не схоже: саме ці кадри й треба пояснити.</p>
{"".join(rows)}
</html>"""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Grouping machine screenshots by screen layout."
    )
    parser.add_argument("source", type=Path, help="folder with frames or a .zip")
    parser.add_argument("--out", type=Path, default=Path("frame_clusters"))
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    args = parser.parse_args()

    if not args.source.exists():
        print(f"not found: {args.source}")
        return 1

    frames = load_frames(args.source)
    if not frames:
        print("no .png frames found")
        return 1

    clusters = cluster_frames(frames, args.threshold)
    args.out.mkdir(parents=True, exist_ok=True)
    for index, cluster in enumerate(clusters, start=1):
        cluster.sample.save(args.out / f"view-{index:02d}.png")

    report = args.out / "clusters.html"
    report.write_text(build_report(clusters, len(frames), args.threshold, str(args.source)), encoding="utf-8")

    # Консоль Windows буває в cp1251 — друкуємо лише ASCII, інакше збирач
    # "падає на друці", а для оператора це те саме, що "не працює".
    print(f"frames: {len(frames)}  views: {len(clusters)}")
    for index, cluster in enumerate(clusters, start=1):
        gap = distance(cluster.signature, clusters[0].signature)
        print(f"  view {index:2d}: {len(cluster.members):4d} frames  gap={gap:5.1f}  e.g. {cluster.sample_name}")
    print(f"report: {report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
