"""Навчання «аварія й відновлення» (DR drill) — на ізольованих копіях, не на проді.

  python scripts/dr_drill.py [--only s1,s3] [--keep] [--root <тека>]

Береться ЖИВА dev-база (через VACUUM INTO — узгоджений знімок) і її справжні
знімки з ./backups; кожен сценарій отримує СВОЮ пісочницю (тека даних, база,
ключ шифрування, порти) і ганяється в дочірньому процесі, щоб env конфігу не
протікав. Прод і жива dev-база не змінюються. Інтеграції (пошта, Google,
Telegram, верстати, печі) у пісочницях ВИМКНЕНІ (`neutralize`): відновлена
база несе справжні реквізити, і застосунок з ними пішов би в реальну скриньку.

Статуси: PASS — система повелась правильно; GAP — небезпечно/мовчки (знахідка);
INFO — спостереження; FAIL — збій самого навчання. Звіт: <root>/report.md.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import sqlite3
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PY = REPO / ".venv" / "Scripts" / "python.exe"
RESULTS: list[dict] = []


# ── Інфраструктура ──────────────────────────────────────────────────────────
def dev_key() -> str:
    for line in (REPO / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("DB_ENCRYPTION_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("немає DB_ENCRYPTION_KEY у .env")


def child_env(sb: Path, key: str, port: int | None = None) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("KUUBMILL_", "ORDER_DESK_", "DB_", "GOOGLE_", "SESSION_"))}
    env.update(
        KUUBMILL_DATA_DIR=str(sb), DB_PATH=str(sb / "kuubmill.db"), DB_ENCRYPTION_KEY=key,
        SESSION_SECRET_KEY="dr-drill-session-key-not-secret", KUUBMILL_NONINTERACTIVE="1",
        GOOGLE_SHEET_ID="", GOOGLE_SERVICE_ACCOUNT_JSON="",
        EXPORT_FOLDER_PATH=str(sb / "export"), MAIL_ATTACHMENTS_PATH=str(sb / "mail"),
        SHIFT_IMAGES_PATH=str(sb / "shift"), FEEDBACK_IMAGES_PATH=str(sb / "feedback"),
        FURNACE_FRAMES_PATH=str(sb / "ff"), MACHINE_FRAMES_PATH=str(sb / "mf"),
        MACHINE_PORTRAITS_PATH=str(sb / "mp"), MACHINE_CALIBRATION_PATH=str(sb / "mc"),
        PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
    )
    return env


def run(code: str, sb: Path, key: str, timeout: int = 180, extra: dict | None = None):
    env = child_env(sb, key)
    env.update(extra or {})
    p = subprocess.run([str(PY), "-c", code], cwd=REPO, env=env, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=timeout)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def jrun(code: str, sb: Path, key: str, **kw) -> dict:
    rc, out, err = run(code, sb, key, **kw)
    last = out.splitlines()[-1] if out else ""
    try:
        data = json.loads(last)
    except Exception:
        data = {"_unparsed": out[-400:]}
    data["_rc"] = rc
    if rc != 0:
        data["_err"] = (err.splitlines()[-1] if err else "")[:300]
    return data


def sandbox(root: Path, name: str, src_db: Path | None = None, backups: bool = False) -> Path:
    sb = root / name
    if sb.exists():
        shutil.rmtree(sb)
    sb.mkdir(parents=True)
    for d in ("export", "mail", "backups"):
        (sb / d).mkdir()
    if src_db:
        shutil.copyfile(src_db, sb / "kuubmill.db")
    if backups:
        shutil.copytree(REPO / "backups", sb / "backups", dirs_exist_ok=True)
    return sb


def record(sid: str, title: str, status: str, detail: str, extra: dict | None = None):
    row = {"id": sid, "title": title, "status": status, "detail": detail, **(extra or {})}
    RESULTS.append(row)
    print(f"[{status:4}] {sid} {title}: {detail}", flush=True)


def scalar(db: Path, sql: str):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return con.execute(sql).fetchone()[0]
    finally:
        con.close()


NEUTRALIZE = r"""
import sqlite3, sys
con = sqlite3.connect(sys.argv[1]) if len(sys.argv) > 1 else None
"""


def neutralize(db: Path):
    """Вимкнути інтеграції в пісочниці: лишається лише ліцензія."""
    con = sqlite3.connect(db)
    try:
        names = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
        if "app_settings" in names:
            con.execute("delete from app_settings where key not in ('license_key')")
        for t in ("machines", "furnaces", "telegram_members", "telegram_invites"):
            if t in names:
                con.execute(f"delete from {t}")
        con.commit()
    finally:
        con.close()


def start_app(sb: Path, key: str, port: int, wait: int = 40, extra: dict | None = None) -> dict:
    env = child_env(sb, key)
    env.update(extra or {})
    log = sb / "app.log"
    with open(log, "w", encoding="utf-8") as fh:
        p = subprocess.Popen([str(PY), "-m", "uvicorn", "app.web:app", "--host", "127.0.0.1", "--port", str(port)],
                             cwd=REPO, env=env, stdout=fh, stderr=subprocess.STDOUT)
    out = {"port": port}
    try:
        for _ in range(wait):
            time.sleep(1)
            if p.poll() is not None:
                out["died"] = p.returncode
                break
            try:
                r = urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3)
                out["health"] = r.status
                break
            except urllib.error.HTTPError as e:
                out["health"] = e.code
                break
            except Exception:
                continue
        if "health" in out:
            for path in ("/login", "/handout"):
                try:
                    out[path] = urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5).status
                except urllib.error.HTTPError as e:
                    out[path] = e.code
                except Exception as e:  # noqa: BLE001
                    out[path] = type(e).__name__
    finally:
        if p.poll() is None:
            p.kill()
        p.wait()
    tail = log.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-4:]
    out["log_tail"] = " | ".join(t[-160:] for t in tail)
    return out


def _norm(v):
    """Час після відновлення пишеться з мікросекундами (`...43.000000`) — той самий момент."""
    if isinstance(v, str) and len(v) > 7 and v.endswith(".000000"):
        return v[:-7]
    return v


def table_hashes(db: Path, skip_cols_suffix: str = "_encrypted") -> dict:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    out = {}
    try:
        for (t,) in con.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name"):
            if t in ("alembic_version", "app_settings"):
                continue
            # за іменем, а не за позицією: у старій базі колонки, додані міграціями, стоять у кінці
            cols = sorted(c[1] for c in con.execute(f"pragma table_info({t})") if not c[1].endswith(skip_cols_suffix))
            if not cols:
                continue
            rows = sorted(repr(tuple(_norm(v) for v in r)) for r in con.execute(f"select {', '.join(cols)} from {t}"))
            out[t] = (len(rows), hashlib.sha256("\n".join(rows).encode()).hexdigest()[:12])
    finally:
        con.close()
    return out


def make_source(root: Path) -> Path:
    src = root / "source.db"
    if src.exists():
        src.unlink()
    con = sqlite3.connect(REPO / "kuubmill.db")
    con.execute(f"VACUUM INTO '{src}'")
    con.close()
    return src


# ── S1: аварійне вимкнення посеред запису ───────────────────────────────────
WRITER = r"""
import sqlite3, sys, time
db, ack = sys.argv[1], sys.argv[2]
con = sqlite3.connect(db, timeout=15, isolation_level=None)
con.execute("PRAGMA journal_mode=WAL"); con.execute("PRAGMA busy_timeout=15000")
con.execute("create table if not exists dr_probe(batch integer, i integer, payload text)")
n = 0
while True:
    con.execute("begin immediate")
    for i in range(50):
        con.execute("insert into dr_probe values (?,?,?)", (n, i, "x" * 400))
    con.execute("commit")
    n += 1
    with open(ack, "w") as fh: fh.write(str(n))
"""


def s1_power_kill(root: Path, src: Path, rounds: int = 20):
    sb = sandbox(root, "s1", src)
    db, ack = sb / "kuubmill.db", sb / "ack.txt"
    orders0 = scalar(db, "select count(*) from orders")
    bad = []
    prev_total = 0
    for r in range(rounds):
        ack.write_text("0")
        p = subprocess.Popen([str(PY), "-c", WRITER, str(db), str(ack)])
        time.sleep(random.uniform(0.3, 1.6))
        p.kill()
        p.wait()
        try:
            acked = int(ack.read_text() or 0)
        except ValueError:
            acked = 0
        con = sqlite3.connect(db, timeout=15)
        ic = con.execute("pragma integrity_check").fetchone()[0]
        total = con.execute("select count(*) from dr_probe").fetchone()[0]
        orders = con.execute("select count(*) from orders").fetchone()[0]
        con.close()
        # скільки підтверджених пачок має бути: загальна к-сть, кратна 50 (немає напівпачок)
        # кожна ПІДТВЕРДЖЕНА (ack записано після COMMIT) пачка мусить пережити kill
        if ic != "ok" or total % 50 or orders != orders0 or total - prev_total < acked * 50:
            bad.append((r, ic, total, orders, acked))
        prev_total = total
        # дати наступному раунду «чистий» ack: рахунок продовжується з поточного стану
    n_total = scalar(db, "select count(*) from dr_probe")
    if bad:
        record("S1", "kill -9 посеред запису ×%d" % rounds, "GAP", f"зіпсовано/напівпачки: {bad[:3]}")
    else:
        record("S1", "kill -9 посеред запису ×%d" % rounds, "PASS",
               f"integrity_check ok щоразу, жодної напівтранзакції, orders не змінились ({orders0}); записано {n_total} рядків. "
               "Обмеження: це смерть процесу, не втрата живлення ОС — fsync не перевірено.")


# ── S2: пастка копіювання файлу бази без WAL ────────────────────────────────
S2_CODE = r"""
import json, shutil, sqlite3, sys
from pathlib import Path
db = Path(sys.argv[1]); out = Path(sys.argv[2])
con = sqlite3.connect(db, isolation_level=None); con.execute("PRAGMA journal_mode=WAL"); con.execute("PRAGMA wal_autocheckpoint=0")
con.execute("create table if not exists dr_probe(i integer)")
con.execute("begin"); [con.execute("insert into dr_probe values (?)", (i,)) for i in range(500)]; con.execute("commit")
naive = out / "naive.db"; shutil.copy2(db, naive)
from app.schema import backup_database
good = backup_database(db, out / "bk")
def cnt(p):
    try:
        c = sqlite3.connect(p); n = c.execute("select count(*) from dr_probe").fetchone()[0]; c.close(); return n
    except Exception as e:
        return "ERR " + type(e).__name__
print(json.dumps({"live": 500, "naive_file_copy": cnt(naive), "backup_database": cnt(good) if good else None}))
"""


def s2_copy_trap(root: Path, src: Path, key: str):
    sb = sandbox(root, "s2", src)
    (sb / "out").mkdir(exist_ok=True)
    d = jrun(S2_CODE.replace("sys.argv[2]", repr(str(sb / "out"))).replace("sys.argv[1]", repr(str(sb / "kuubmill.db"))), sb, key)
    if d.get("backup_database") == 500:
        record("S2", "копія файлу бази без WAL vs backup_database", "PASS",
               f"наївне копіювання файлу: {d.get('naive_file_copy')} з 500 рядків (пастка реальна); backup_database (VACUUM INTO): 500")
    else:
        record("S2", "копія файлу бази без WAL vs backup_database", "GAP", json.dumps(d, ensure_ascii=False))


# ── S3: старт на пошкодженій / зниклій базі ─────────────────────────────────
START_CODE = r"""
import json, sqlite3, sys
from pathlib import Path
db = Path(sys.argv[1]); res = {}
try:
    from app.schema import ensure_schema
    ensure_schema(db, db.parent / "backups")
    res["ensure_schema"] = "ok"
except Exception as e:
    res["ensure_schema"] = type(e).__name__ + ": " + str(e)[:140]
try:
    con = sqlite3.connect(db); res["integrity"] = con.execute("pragma integrity_check").fetchone()[0][:80]
    res["orders"] = con.execute("select count(*) from orders").fetchone()[0]; con.close()
except Exception as e:
    res["read"] = type(e).__name__ + ": " + str(e)[:120]
print(json.dumps(res, ensure_ascii=False))
"""


def damage(kind: str, db: Path, src: Path):
    for ext in ("-wal", "-shm"):
        p = Path(str(db) + ext)
        if p.exists():
            p.unlink()
    if kind == "missing":
        db.unlink()
    elif kind == "zero":
        db.write_bytes(b"")
    elif kind == "garbage":
        db.write_bytes(os.urandom(4_000_000))
    elif kind == "truncated":
        data = db.read_bytes()
        db.write_bytes(data[: len(data) // 2])
    elif kind == "mid_corrupt":
        con = sqlite3.connect(db)
        ps = con.execute("pragma page_size").fetchone()[0]
        root = con.execute("select rootpage from sqlite_master where name='orders'").fetchone()[0]
        con.close()
        with open(db, "r+b") as fh:
            fh.seek((root - 1) * ps + 100)
            fh.write(os.urandom(ps - 200))
    elif kind == "header_zeroed":
        with open(db, "r+b") as fh:
            fh.write(b"\x00" * 100)


def s3_startup(root: Path, src: Path, key: str):
    for kind in ("missing", "zero", "garbage", "truncated", "mid_corrupt", "header_zeroed"):
        sb = sandbox(root, f"s3-{kind}", src, backups=True)
        damage(kind, sb / "kuubmill.db", src)
        d = jrun(START_CODE.replace("sys.argv[1]", repr(str(sb / "kuubmill.db"))), sb, key)
        snaps = len(list((sb / "backups").rglob("*.db")))
        ens = d.get("ensure_schema", "")
        orders = d.get("orders")
        integ = d.get("integrity", "")
        fresh_silent = ens == "ok" and orders == 0
        if fresh_silent:
            record(f"S3-{kind}", f"старт: база {kind}", "GAP",
                   f"ПОРОЖНЯ база створена мовчки, orders=0, хоча в backups є {snaps} знімків; жодної спроби відновлення")
        elif ens == "ok" and (integ != "ok" or "read" in d):
            record(f"S3-{kind}", f"старт: база {kind}", "GAP",
                   f"ensure_schema ok, але база зіпсована: integrity={integ!r} {d.get('read','')}; застосунок стартував би на ній")
        elif ens != "ok":
            record(f"S3-{kind}", f"старт: база {kind}", "PASS",
                   f"гучна відмова ({ens[:90]}); дані не змінені; автовідновлення зі знімка немає (знімків: {snaps})")
        else:
            record(f"S3-{kind}", f"старт: база {kind}", "INFO", json.dumps(d, ensure_ascii=False))


def s3b_stale_wal(root: Path, src: Path, key: str):
    """Відновили файл бази зі знімка вручну, а старий -wal лишився поруч."""
    sb = sandbox(root, "s3-stalewal", src)
    live = sb / "kuubmill.db"
    code = (
        "import sqlite3,sys;"
        "c=sqlite3.connect(sys.argv[1],isolation_level=None);c.execute('PRAGMA journal_mode=WAL');c.execute('PRAGMA wal_autocheckpoint=0');"
        "c.execute('create table if not exists dr_probe(i integer)');c.execute('begin');"
        "[c.execute('insert into dr_probe values (?)',(i,)) for i in range(2000)];c.execute('commit');import os;os._exit(0)"
    )
    subprocess.run([str(PY), "-c", code, str(live)], check=True)
    wal = Path(str(live) + ".-wal".replace(".-", "-"))
    had_wal = wal.exists() and wal.stat().st_size > 0
    old_snapshot = REPO / "backups" / "monthly" / "kuubmill-2026-09.db"
    shutil.copyfile(old_snapshot, live)  # «відновлення»: підмінили лише .db
    d = jrun(START_CODE.replace("sys.argv[1]", repr(str(live))), sb, key)
    snap_orders = scalar(old_snapshot, "select count(*) from orders")
    ok = d.get("integrity") == "ok" and d.get("orders") == snap_orders
    record("S3-stalewal", "відновлення підміною .db при чужому -wal поруч",
           "PASS" if ok else "GAP",
           f"-wal був ({had_wal}); результат: integrity={d.get('integrity')}, orders={d.get('orders')} (знімок має {snap_orders}), ensure={d.get('ensure_schema')}")


def s3c_app_health(root: Path, src: Path, key: str):
    """Живий застосунок на частково пошкодженій базі: чи скаже /health, що щось не так."""
    sb = sandbox(root, "s3-health", src)
    damage("mid_corrupt", sb / "kuubmill.db", src)
    # пошкоджено сторінку orders, але інші таблиці читаються → стартує?
    out = start_app(sb, key, 8031)
    if out.get("died") is not None:
        record("S3-health", "застосунок на базі з биттю сторінкою orders", "PASS",
               f"не стартував (код {out['died']}): {out.get('log_tail','')[:160]}")
    elif out.get("health") == 200:
        record("S3-health", "застосунок на базі з биттю сторінкою orders", "GAP",
               f"/health=200, /login={out.get('/login')} — застосунок «здоровий» на зіпсованій базі; помилки вилізуть на першому запиті до orders")
    else:
        record("S3-health", "застосунок на базі з биттю сторінкою orders", "INFO", json.dumps(out, ensure_ascii=False))


# ── S4: усі справжні знімки → вперед до поточної схеми ──────────────────────
FWD_CODE = r"""
import json, sqlite3, sys, time
from pathlib import Path
db = Path(sys.argv[1]); res = {}
con = sqlite3.connect(db)
res["integrity_before"] = con.execute("pragma integrity_check").fetchone()[0][:60]
names = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
res["orders_before"] = con.execute("select count(*) from orders").fetchone()[0] if "orders" in names else None
res["rev_before"] = con.execute("select version_num from alembic_version").fetchone()[0] if "alembic_version" in names else None
con.close()
t = time.time()
try:
    from app.schema import ensure_schema
    ensure_schema(db, db.parent / "bk")
    res["ensure"] = "ok"
except Exception as e:
    res["ensure"] = type(e).__name__ + ": " + str(e)[:160]
res["sec"] = round(time.time() - t, 1)
try:
    con = sqlite3.connect(db)
    res["rev_after"] = con.execute("select version_num from alembic_version").fetchone()[0]
    res["orders_after"] = con.execute("select count(*) from orders").fetchone()[0]
    res["integrity_after"] = con.execute("pragma integrity_check").fetchone()[0][:60]
except Exception as e:
    res["after_err"] = str(e)[:100]
print(json.dumps(res, ensure_ascii=False))
"""


def s4_snapshots_forward(root: Path, key: str):
    files = sorted(p for p in (REPO / "backups").rglob("*.db") if p.stat().st_size > 0)
    for p in files:
        rel = p.relative_to(REPO / "backups").as_posix()
        sb = sandbox(root, "s4-" + rel.replace("/", "_").replace(".db", ""))
        shutil.copyfile(p, sb / "kuubmill.db")
        d = jrun(FWD_CODE.replace("sys.argv[1]", repr(str(sb / "kuubmill.db"))), sb, key)
        ok = d.get("ensure") == "ok" and d.get("orders_after") == d.get("orders_before") and d.get("integrity_after") == "ok"
        rev = str(d.get("rev_before"))[:22]
        record("S4", f"знімок {rel} → поточна схема", "PASS" if ok else "GAP",
               f"orders {d.get('orders_before')}→{d.get('orders_after')}, ревізія {rev}→{str(d.get('rev_after'))[:22]}, "
               f"{d.get('sec')} с, ensure={d.get('ensure')[:80] if isinstance(d.get('ensure'), str) else d.get('ensure')}")


# ── S5: міграція перервана посеред ──────────────────────────────────────────
INTERRUPT_CODE = r"""
import json, sqlite3, sys
from pathlib import Path
import alembic.command as cmd
db = Path(sys.argv[1]); res = {}
real = cmd.upgrade; calls = {"n": 0}
def boom(cfg, rev, *a, **k):
    calls["n"] += 1
    if calls["n"] == 3:
        raise KeyboardInterrupt("drill: процес убито посеред міграції")
    return real(cfg, rev, *a, **k)
cmd.upgrade = boom
from app import schema
try:
    schema.ensure_schema(db, db.parent / "bk")
except BaseException as e:
    res["first_run"] = type(e).__name__
con = sqlite3.connect(db); res["rev_mid"] = con.execute("select version_num from alembic_version").fetchone()[0][:24]
res["orders_mid"] = con.execute("select count(*) from orders").fetchone()[0]; con.close()
cmd.upgrade = real
try:
    schema.ensure_schema(db, db.parent / "bk"); res["second_run"] = "ok"
except Exception as e:
    res["second_run"] = type(e).__name__ + ": " + str(e)[:160]
con = sqlite3.connect(db); res["rev_end"] = con.execute("select version_num from alembic_version").fetchone()[0][:24]
res["orders_end"] = con.execute("select count(*) from orders").fetchone()[0]
res["integrity"] = con.execute("pragma integrity_check").fetchone()[0]; con.close()
res["backups"] = len(list((db.parent / "bk").glob("*.db")))
print(json.dumps(res, ensure_ascii=False))
"""


def s5_interrupted_migration(root: Path, key: str):
    old = REPO / "backups" / "monthly" / "kuubmill-2026-08.db"   # ревізія 0047: десятки кроків до голови
    sb = sandbox(root, "s5")
    shutil.copyfile(old, sb / "kuubmill.db")
    d = jrun(INTERRUPT_CODE.replace("sys.argv[1]", repr(str(sb / "kuubmill.db"))), sb, key)
    ok = d.get("second_run") == "ok" and d.get("integrity") == "ok" and d.get("orders_end") == d.get("orders_mid")
    ok = ok and d.get("first_run") == "KeyboardInterrupt"   # переривання справді відбулось
    record("S5", "міграція вбита на 3-му кроці, повторний старт", "PASS" if ok else "GAP",
           f"перша спроба: {d.get('first_run')} на {d.get('rev_mid')}; друга: {d.get('second_run')}, "
           f"кінцева {d.get('rev_end')}, orders {d.get('orders_mid')}→{d.get('orders_end')}, integrity={d.get('integrity')}, знімків перед міграцією: {d.get('backups')}")


# ── S6/S7: «новий ПК» — переносний експорт → чиста інсталяція з ІНШИМ ключем ──
EXPORT_CODE = r"""
import json, sys
from pathlib import Path
from sqlalchemy.orm import Session
from app.db import engine
from app.backup import create_backup
from app.settings_store import SECRET_SETTING_KEYS, get_setting
with Session(engine) as db:
    blob = create_backup(db, "drill-password-123")
    Path(sys.argv[1]).write_bytes(blob)
    present = {k: bool(get_setting(db, k)) for k in sorted(SECRET_SETTING_KEYS)}
    import hashlib
    sec = {k: hashlib.sha256((get_setting(db, k) or "").encode()).hexdigest()[:12] for k in sorted(SECRET_SETTING_KEYS)}
print(json.dumps({"bytes": len(blob), "secrets_present": present, "secret_hash": sec}))
"""

IMPORT_CODE = r"""
import json, sys, hashlib
from pathlib import Path
from sqlalchemy.orm import Session
from app.schema import ensure_schema
ensure_schema(Path(sys.argv[2]), Path(sys.argv[2]).parent / "backups")
from app.db import engine
from app.backup import restore_backup
from app.settings_store import SECRET_SETTING_KEYS, get_setting
res = {}
with Session(engine) as db:
    try:
        restore_backup(db, Path(sys.argv[1]).read_bytes(), sys.argv[3])
        res["restore"] = "ok"
    except Exception as e:
        res["restore"] = type(e).__name__ + ": " + str(e)[:140]
    res["secret_hash"] = {k: hashlib.sha256((get_setting(db, k) or "").encode()).hexdigest()[:12] for k in sorted(SECRET_SETTING_KEYS)}
print(json.dumps(res, ensure_ascii=False))
"""


def s6_new_pc_restore(root: Path, src: Path, key: str):
    from cryptography.fernet import Fernet

    srcsb = sandbox(root, "s6-source", src)
    exp = srcsb / "export.kmb"
    d = jrun(EXPORT_CODE.replace("sys.argv[1]", repr(str(exp))), srcsb, key)
    if not exp.exists():
        record("S6", "експорт для нового ПК", "FAIL", json.dumps(d, ensure_ascii=False)[:300])
        return
    new_key = Fernet.generate_key().decode()  # «інший ПК»: інший ключ шифрування
    tgt = sandbox(root, "s6-newpc")
    d2 = jrun(IMPORT_CODE.replace("sys.argv[1]", repr(str(exp))).replace("sys.argv[2]", repr(str(tgt / "kuubmill.db")))
              .replace("sys.argv[3]", repr("drill-password-123")), tgt, new_key)
    # орієнтир: експорт робить сам застосунок на ТОМУ Ж джерелі, тож порівнюємо таблиці знімка з відновленою базою
    h_src, h_tgt = table_hashes(src), table_hashes(tgt / "kuubmill.db")
    # таблиці, яких уже нема в моделях (сироти старої бази), — не втрата
    diff = {t: (h_src.get(t), h_tgt.get(t)) for t in set(h_src) & set(h_tgt) if h_src.get(t) != h_tgt.get(t)}
    legacy_only = sorted(set(h_src) - set(h_tgt))
    secrets_ok = d.get("secret_hash") == d2.get("secret_hash")
    n_secrets = sum(1 for v in d.get("secrets_present", {}).values() if v)
    # різниця лише в журналах дій (restore пише рядок) — не втрата
    benign = {t for t in diff if t in ("action_log", "action_logs", "sync_log", "sync_logs")}
    real = {t: v for t, v in diff.items() if t not in benign}
    status = "PASS" if (d2.get("restore") == "ok" and not real and secrets_ok) else "GAP"
    record("S6", "новий ПК: переносний експорт → чиста інсталяція з іншим ключем", status,
           f"restore={d2.get('restore')}; таблиць {len(h_src)}, відмінних (крім журналів) {len(real)} {list(real)[:4]}; "
           f"секретів у джерелі {n_secrets}, після відновлення збігаються під НОВИМ ключем: {secrets_ok}; сироти старої бази (не в моделях): {legacy_only}")
    # застосунок стартує на відновленій базі
    neutralize(tgt / "kuubmill.db")
    out = start_app(tgt, new_key, 8032)
    record("S6b", "застосунок стартує на відновленій базі (інтеграції вимкнено)",
           "PASS" if out.get("health") == 200 else "GAP", json.dumps({k: v for k, v in out.items() if k != "log_tail"}, ensure_ascii=False))
    # S7: погані експорти
    blob = exp.read_bytes()
    cases = {
        "неправильний пароль": (blob, "wrong-password-x"),
        "сміття замість файлу": (os.urandom(5000), "drill-password-123"),
        "обрізаний файл": (blob[: len(blob) // 2], "drill-password-123"),
        "один змінений байт": (blob[:1000] + bytes([blob[1000] ^ 1]) + blob[1001:], "drill-password-123"),
    }
    for name, (data, pw) in cases.items():
        sb = sandbox(root, "s7", src)
        before = table_hashes(sb / "kuubmill.db")
        bad = sb / "bad.kmb"
        bad.write_bytes(data)
        d3 = jrun(IMPORT_CODE.replace("sys.argv[1]", repr(str(bad))).replace("sys.argv[2]", repr(str(sb / "kuubmill.db")))
                  .replace("sys.argv[3]", repr(pw)), sb, key)
        after = table_hashes(sb / "kuubmill.db")
        refused = d3.get("restore") != "ok"
        untouched = before == after
        record("S7", f"експорт: {name}", "PASS" if (refused and untouched) else "GAP",
               f"відмова={refused} ({str(d3.get('restore'))[:70]}), база не змінилась={untouched}")


# ── S8: втрата ключа шифрування (інший користувач Windows, видалений master.key) ──
KEYLOSS_CODE = r"""
import json
from sqlalchemy.orm import Session
from sqlalchemy import text
from app.db import engine
from app.settings_store import get_setting, setting_unreadable, set_setting, SECRET_SETTING_KEYS
res = {}
with Session(engine) as db:
    keys = [k for (k,) in db.execute(text("select key from app_settings"))]
    res["settings_total"] = len(keys)
    res["unreadable"] = sum(1 for k in keys if setting_unreadable(db, k))
    res["read_returns_none"] = all(get_setting(db, k) is None for k in keys if setting_unreadable(db, k))
    try:
        from app.license import get_license_status
        st = get_license_status(db)
        res["license"] = {"key_error": getattr(st, "key_error", None), "valid": getattr(st, "valid", None)}
    except Exception as e:
        res["license"] = type(e).__name__ + ": " + str(e)[:100]
    try:
        from app.services.machines import target_of
        from app.models import Machine
        ms = db.query(Machine).all()
        for m in ms:
            target_of(m)
        res["machines_checked"] = len(ms)
    except Exception as e:
        res["machines_err"] = type(e).__name__ + ": " + str(e)[:100]
    set_setting(db, "mail_processed_folder", "drill-folder"); db.commit()
    res["reentry_readable"] = get_setting(db, "mail_processed_folder") == "drill-folder"
print(json.dumps(res, ensure_ascii=False))
"""


def s8_key_loss(root: Path, src: Path, key: str):
    from cryptography.fernet import Fernet

    sb = sandbox(root, "s8", src)
    wrong = Fernet.generate_key().decode()
    d = jrun(KEYLOSS_CODE, sb, wrong)
    ok = (d.get("_rc") == 0 and d.get("unreadable", 0) > 0 and d.get("read_returns_none") and d.get("reentry_readable"))
    record("S8", "втрата ключа шифрування (master.key нечитний/інший)", "PASS" if ok else "GAP",
           json.dumps({k: v for k, v in d.items() if not k.startswith("_")}, ensure_ascii=False)[:360])
    neutralize(sb / "kuubmill.db")
    out = start_app(sb, wrong, 8033)
    record("S8b", "застосунок стартує з чужим ключем (секрети нечитні)", "PASS" if out.get("health") == 200 else "GAP",
           json.dumps({k: v for k, v in out.items() if k != "log_tail"}, ensure_ascii=False))


# ── S9: резервне копіювання з недоступною ціллю ─────────────────────────────
def s9_backup_target_broken(root: Path, src: Path, key: str):
    sb = sandbox(root, "s9", src)
    (sb / "backups").rmdir()
    (sb / "backups").write_text("це файл, а не тека")   # диск «повний/недоступний»: цілі немає
    code = (
        "import json,sys\nfrom pathlib import Path\nfrom datetime import date\n"
        "from app.db import engine\nfrom app.monthly_backup import ensure_monthly_snapshot\n"
        "r={}\ntry:\n  r['monthly']=str(ensure_monthly_snapshot(engine, Path(sys.argv[1]), date(2026,11,5)))\n"
        "except Exception as e:\n  r['monthly']=type(e).__name__+': '+str(e)[:100]\nprint(json.dumps(r,ensure_ascii=False))"
    ).replace("sys.argv[1]", repr(str(sb / "kuubmill.db")))
    d = jrun(code, sb, key)
    record("S9", "місячний знімок, коли цільова тека недоступна", "INFO", json.dumps({k: v for k, v in d.items() if not k.startswith("_")}, ensure_ascii=False))

# ── S10: чиста інсталяція рівно як у лаунчері ───────────────────────────────
LAUNCHER_MIGRATE = (
    "import os\nimport app.windows_launcher as wl\nwl.DB_PATH = os.environ['DB_PATH']\n"
    "wl._run_migrations()\nprint('migrated')"
)


def s10_fresh_install_like_launcher(root: Path, key: str):
    """Нова інсталяція на чистому ПК: міграції ДО імпорту застосунку, потім
    застосунок із KUUBMILL_SCHEMA_MANAGED=1 (lifespan міграцій не робить)."""
    sb = sandbox(root, "s10")
    rc, out, err = run(LAUNCHER_MIGRATE, sb, key)
    n_tables = scalar(sb / "kuubmill.db", "select count(*) from sqlite_master where type='table'")
    res = start_app(sb, key, 8035, extra={"KUUBMILL_SCHEMA_MANAGED": "1"})
    ok = rc == 0 and n_tables > 30 and res.get("/login") == 200 and res.get("/handout") in (200, 302, 303, 401)
    record("S10", "чиста інсталяція як у лаунчері (нова база → /login)", "PASS" if ok else "GAP",
           f"таблиць у новій базі: {n_tables}; health={res.get('health')}, /login={res.get('/login')}, /handout={res.get('/handout')}"
           + ("" if rc == 0 else f"; міграції rc={rc} {err[-120:]}"))

# ── S11: відновлення кожного справжнього знімка, коли поруч чужий -wal ──────
RESTORE_CODE = r"""
import json, sys
from pathlib import Path
from app.snapshot_tools import restore_from_snapshot, inspect_snapshot
live, snap = Path(sys.argv[1]), Path(sys.argv[2])
res = {"wal_before": Path(str(live) + "-wal").exists() and Path(str(live) + "-wal").stat().st_size}
try:
    rep = restore_from_snapshot(live, snap, running=lambda: False)
    res["restore"] = "ok"
    info = inspect_snapshot(live)
    res["orders"], res["ok"], res["rev"] = info.orders, info.ok, (info.revision or "")[:18]
    res["aside"] = len(rep["kept_aside"])
except Exception as e:
    res["restore"] = type(e).__name__ + ": " + str(e)[:120]
print(json.dumps(res, ensure_ascii=False))
"""


def _stale_wal_live(sb: Path) -> bool:
    """Жива база, вбита посеред запису: лишається непорожній -wal."""
    for _ in range(5):
        db, ack = sb / "kuubmill.db", sb / "ack.txt"
        p = subprocess.Popen([str(PY), "-c", WRITER, str(db), str(ack)])
        time.sleep(0.8)
        p.kill()
        p.wait()
        wal = Path(str(db) + "-wal")
        if wal.exists() and wal.stat().st_size > 0:
            return True
    return False


def s11_restore_real_snapshots(root: Path, src: Path, key: str):
    files = sorted(p for p in (REPO / "backups").rglob("*.db") if p.stat().st_size > 0 and "orderdesk-2026-07" not in p.name)
    for p in files:
        rel = p.relative_to(REPO / "backups").as_posix()
        sb = sandbox(root, "s11-" + rel.replace("/", "_").replace(".db", ""), src)
        had_wal = _stale_wal_live(sb)
        d = jrun(RESTORE_CODE.replace("sys.argv[1]", repr(str(sb / "kuubmill.db"))).replace("sys.argv[2]", repr(str(p))), sb, key)
        snap_orders = scalar(p, "select count(*) from orders")
        # і після цього застосунок доводить базу до поточної версії
        m = jrun(FWD_CODE.replace("sys.argv[1]", repr(str(sb / "kuubmill.db"))), sb, key)
        ok = (d.get("restore") == "ok" and d.get("orders") == snap_orders and d.get("ok")
              and m.get("ensure") == "ok" and m.get("integrity_after") == "ok" and m.get("orders_after") == snap_orders)
        record("S11", f"відновлення {rel} при чужому -wal", "PASS" if ok else "GAP",
               f"-wal був={bool(had_wal)}; orders {d.get('orders')} (знімок {snap_orders}); після старту: ensure={m.get('ensure')}, "
               f"orders={m.get('orders_after')}, integrity={m.get('integrity_after')}; стару базу збережено поруч: {d.get('aside')} файл(и)")


# ── S12: смерть ПК — нова тека даних, відновлення з дзеркала на іншому диску ─
def s12_pc_dead_mirror(root: Path, src: Path, key: str):
    from cryptography.fernet import Fernet

    mirror = sandbox(root, "s12-mirror")
    (mirror / "daily").mkdir()
    # дзеркало, як його наповнюють воркери: monthly + pre-update + daily
    shutil.copytree(REPO / "backups" / "monthly", mirror / "monthly", dirs_exist_ok=True)
    shutil.copytree(REPO / "backups" / "pre-update", mirror / "pre-update", dirs_exist_ok=True)
    shutil.copyfile(src, mirror / "daily" / "kuubmill-daily-2026-10-07.db")
    # ПК помер: з нього лишився лише диск дзеркала. Нова тека даних і ІНШИЙ ключ (новий ПК)
    new = sandbox(root, "s12-newpc")
    new_key = Fernet.generate_key().decode()
    code = r"""
import json, sys
from pathlib import Path
from app import snapshot_cli
said = []
rc = snapshot_cli.run(list_only=False, target="latest", data_dir=Path(sys.argv[1]), db_file=Path(sys.argv[1]) / "kuubmill.db",
                      from_dirs=[sys.argv[2]], say=said.append, ask=lambda t: True, running=lambda: False)
print(json.dumps({"rc": rc, "said": said[-1][:200] if said else ""}, ensure_ascii=False))
""".replace("sys.argv[1]", repr(str(new))).replace("sys.argv[2]", repr(str(mirror)))
    d = jrun(code, new, new_key)
    mig = jrun(LAUNCHER_MIGRATE_JSON, new, new_key)
    orders = scalar(new / "kuubmill.db", "select count(*) from orders") if (new / "kuubmill.db").exists() else None
    unreadable = jrun(KEYLOSS_CODE, new, new_key)
    neutralize(new / "kuubmill.db")
    out = start_app(new, new_key, 8036, extra={"KUUBMILL_SCHEMA_MANAGED": "1"})
    ok = d.get("rc") == 0 and orders == scalar(src, "select count(*) from orders") and out.get("health") == 200 and out.get("/login") == 200
    record("S12", "смерть ПК: порожня тека даних + дзеркало → відновлення → старт", "PASS" if ok else "GAP",
           f"cli rc={d.get('rc')}; відновлено orders={orders}; міграції: {mig.get('m')}; /health={out.get('health')}, /login={out.get('/login')}; "
           f"секретів нечитних під новим ключем: {unreadable.get('unreadable')} з {unreadable.get('settings_total')} "
           "(очікувано: потрібно ввести IMAP/Google/ліцензію наново АБО відновитись переносним експортом)")


LAUNCHER_MIGRATE_JSON = (
    "import json, os\nimport app.windows_launcher as wl\nwl.DB_PATH = os.environ['DB_PATH']\n"
    "wl._run_migrations()\nprint(json.dumps({'m': 'ok'}))"
)

# ── S13: наскрізно через HTTP — чиста інсталяція → /setup → відновлення в UI ─
def _multipart(fields: dict, file_field: str, filename: str, data: bytes) -> tuple[bytes, str]:
    boundary = "----drill" + hashlib.sha1(os.urandom(8)).hexdigest()[:12]
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
                  "Content-Type: application/octet-stream\r\n\r\n").encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def s13_http_restore_flow(root: Path, src: Path, key: str):
    import http.cookiejar
    import urllib.parse

    from cryptography.fernet import Fernet

    # джерело з вимкненими інтеграціями: відновлений застосунок не піде в реальну скриньку/таблицю
    source = sandbox(root, "s13-source", src)
    neutralize(source / "kuubmill.db")
    exp = source / "export.kmb"
    jrun(EXPORT_CODE.replace("sys.argv[1]", repr(str(exp))), source, key)
    # нова машина: чиста інсталяція (порядок лаунчера), інший ключ, ліцензію «введено» (на цьому ж ПК вона дійсна)
    new_key = Fernet.generate_key().decode()
    new = sandbox(root, "s13-newpc")
    jrun(LAUNCHER_MIGRATE_JSON, new, new_key)
    lic = jrun(r"""
import json, sys
from sqlalchemy.orm import Session
from app.db import engine
from app.settings_store import get_setting
with Session(engine) as db:
    print(json.dumps({"lic": get_setting(db, "license_key") or ""}))
""", source, key).get("lic", "")
    jrun("import json, sys\nfrom sqlalchemy.orm import Session\nfrom app.db import engine\nfrom app.settings_store import set_setting\n"
         f"with Session(engine) as db:\n    set_setting(db, 'license_key', {lic!r}); db.commit()\nprint(json.dumps({{}}))", new, new_key)

    port = 8037
    env = child_env(new, new_key)
    env["KUUBMILL_SCHEMA_MANAGED"] = "1"
    with open(new / "app.log", "w", encoding="utf-8") as fh:
        p = subprocess.Popen([str(PY), "-m", "uvicorn", "app.web:app", "--host", "127.0.0.1", "--port", str(port)],
                             cwd=REPO, env=env, stdout=fh, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    steps: dict = {}
    try:
        for _ in range(40):
            time.sleep(1)
            try:
                op.open(base + "/health", timeout=3)
                break
            except Exception:
                continue

        def get(path):
            try:
                r = op.open(base + path, timeout=15)
                return r.status, r.geturl(), r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                return e.code, path, ""

        def post(path, fields):
            data = urllib.parse.urlencode(fields).encode()
            try:
                r = op.open(urllib.request.Request(base + path, data=data), timeout=20)
                return r.status, r.geturl(), r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                return e.code, path, ""

        st, url, _ = get("/")
        steps["чиста база → / веде на"] = url.replace(base, "")
        st, url, body = get("/setup")
        steps["/setup"] = st
        pw = "Drill-Admin-2026-x!"
        st, url, _ = post("/setup", {"username": "dr.admin", "full_name": "DR Admin", "password": pw, "password_confirmation": pw})
        steps["створення адміна"] = f"{st} → {url.replace(base, '')}"
        st, url, _ = post("/login", {"username": "dr.admin", "password": pw})
        steps["вхід адміном"] = f"{st} → {url.replace(base, '')}"
        body_bytes, ctype = _multipart({"backup_password": "drill-password-123", "confirm_replace": "on"},
                                       "backup_file", "backup.kmb", exp.read_bytes())
        req = urllib.request.Request(base + "/settings/backup/import", data=body_bytes, headers={"Content-Type": ctype})
        try:
            r = op.open(req, timeout=60)
            steps["відновлення в UI"] = f"{r.status} → {r.geturl().replace(base, '')}"
        except urllib.error.HTTPError as e:
            steps["відновлення в UI"] = f"HTTP {e.code}"
        # після відновлення старий акаунт зник (заміна, не злиття): входимо оператором із відновлених даних
        jar.clear()
        st, url, _ = post("/login", {"username": "claude_test", "password": "claude-test-2026"})
        steps["вхід відновленим оператором"] = f"{st} → {url.replace(base, '')}"
        st, url, body = get("/")
        steps["черга після відновлення"] = f"{st}, рядків-робіт у розмітці: {body.count('data-order-id')}"
        st2, _, _ = get("/handout")
        steps["/handout"] = st2
    finally:
        p.kill()
        p.wait()
    n_orders = scalar(new / "kuubmill.db", "select count(*) from orders")
    n_src = scalar(source / "kuubmill.db", "select count(*) from orders")
    # після відновлення користувачі ЗАМІНЕНІ, тож сесія адміна гине й редирект веде на /login — це норма
    ok = (steps.get("/setup") == 200 and str(steps.get("відновлення в UI", "")).startswith("200") and n_orders == n_src
          and str(steps.get("вхід відновленим оператором", "")).startswith("200") and steps.get("/handout") == 200)
    record("S13", "наскрізно: чиста інсталяція → /setup → відновлення переносного файлу в UI → вхід", "PASS" if ok else "GAP",
           f"orders після відновлення {n_orders} (у джерелі {n_src}); " + "; ".join(f"{k}: {v}" for k, v in steps.items()))


SCENARIOS = {
    "s1": lambda r, s, k: s1_power_kill(r, s),
    "s2": s2_copy_trap,
    "s3": lambda r, s, k: (s3_startup(r, s, k), s3b_stale_wal(r, s, k), s3c_app_health(r, s, k)),
    "s4": lambda r, s, k: s4_snapshots_forward(r, k),
    "s5": lambda r, s, k: s5_interrupted_migration(r, k),
    "s6": s6_new_pc_restore,
    "s8": s8_key_loss,
    "s9": s9_backup_target_broken,
    "s10": lambda r, s, k: s10_fresh_install_like_launcher(r, k),
    "s11": s11_restore_real_snapshots,
    "s12": s12_pc_dead_mirror,
    "s13": s13_http_restore_flow,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--root", default="")
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()
    root = Path(a.root) if a.root else Path(tempfile.mkdtemp(prefix="km-dr-"))
    root.mkdir(parents=True, exist_ok=True)
    print(f"пісочниця: {root}", flush=True)
    key = dev_key()
    src = make_source(root)
    print(f"джерело: {src.name}, {src.stat().st_size/1e6:.1f} МБ, orders={scalar(src, 'select count(*) from orders')}", flush=True)
    chosen = [s for s in (a.only.split(",") if a.only else SCENARIOS) if s in SCENARIOS]
    for s in chosen:
        try:
            SCENARIOS[s](root, src, key)
        except Exception as e:  # noqa: BLE001
            record(s.upper(), "збій навчання", "FAIL", f"{type(e).__name__}: {e}")
    lines = ["| № | Сценарій | Статус | Деталі |", "|---|---|---|---|"]
    for r in RESULTS:
        lines.append(f"| {r['id']} | {r['title']} | {r['status']} | {str(r['detail']).replace('|','/')} |")
    (root / "report.md").write_text("\n".join(lines), encoding="utf-8")
    (root / "report.json").write_text(json.dumps(RESULTS, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nзвіт: {root / 'report.md'}")


if __name__ == "__main__":
    main()
