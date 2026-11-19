"""jarvis-files: read-only search over the NAS for Jarvis (runs ON the NAS).

/volume1 is mounted read-only at /data. Every file is indexed by name; documents
(text, Markdown, PDF, Word, CSV, ...) also get their text indexed (SQLite FTS5).
Sensitive paths are excluded and can never be read, even by exact path.

GET /search?q=...&limit=8      full-text + filename search, with snippets
GET /find?name=...&limit=20    filename search (substring, case-insensitive)
GET /read?path=...&offset=0    readable text of one file, 8000 chars per page
GET /stats                     index size and last run
Every request needs:  Authorization: Bearer <FILES_TOKEN>
"""
import asyncio
import fnmatch
import logging
import os
import pathlib
import sqlite3
import time

from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("files")

ROOT = pathlib.Path(os.getenv("FILES_ROOT", "/data")).resolve()
DB = os.getenv("FILES_DB", "/index/files.db")
TOKEN = os.environ["FILES_TOKEN"]
REINDEX_HOUR = int(os.getenv("REINDEX_HOUR", "2"))
MAX_EXTRACT_MB = float(os.getenv("MAX_EXTRACT_MB", "15"))

# Never indexed, never readable. Matched against the path relative to /volume1 and the file name.
DEFAULT_EXCLUDES = [
    "docker", "docker/*", "@*", "*/@*", "#recycle*", "*/#recycle*", ".Trash*", "*/.Trash*",
    "lost+found", "*/.ssh", "*/.ssh/*", ".ssh/*", "*/.gnupg/*", "*tailscale*state*",
    "*.env", ".env*", "*/.env*", "rclone.conf", "*.kdbx", "*.kdb", "*.pem", "*.key", "*.p12", "*.pfx",
    "id_rsa*", "id_ed25519*", "*.ovpn", "*secrets*", "*.sqlite", "*.db", "*.db-*",
]
EXCLUDES = DEFAULT_EXCLUDES + [e.strip() for e in os.getenv("FILES_EXCLUDE", "").split(",") if e.strip()]

TEXT_EXT = {".txt", ".md", ".markdown", ".csv", ".tsv", ".log", ".json", ".yaml", ".yml", ".ini", ".cfg",
            ".conf", ".toml", ".xml", ".html", ".htm", ".rtf", ".py", ".sh", ".js", ".ts", ".sql", ".nfo"}
DOC_EXT = {".pdf", ".docx"}

state = {"running": False, "last_run": None, "last_count": 0}


def excluded(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    low = rel.lower()
    return any(fnmatch.fnmatch(low, p.lower()) or fnmatch.fnmatch(name.lower(), p.lower()) for p in EXCLUDES)


def db():
    con = sqlite3.connect(DB)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, size INT, mtime REAL, seen REAL)")
    con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(path, name, body, tokenize='porter unicode61')")
    return con


def extract(p: pathlib.Path) -> str:
    ext = p.suffix.lower()
    try:
        if p.stat().st_size > MAX_EXTRACT_MB * 1024 * 1024:
            return ""
        if ext in TEXT_EXT:
            return p.read_text(errors="replace")[:300_000]
        if ext == ".pdf":
            from pypdf import PdfReader
            r = PdfReader(str(p))
            return "\n".join((pg.extract_text() or "") for pg in r.pages[:300])[:300_000]
        if ext == ".docx":
            import docx
            return "\n".join(par.text for par in docx.Document(str(p)).paragraphs)[:300_000]
    except Exception as e:
        log.debug("extract failed %s: %s", p, e)
    return ""


def reindex():
    t0, now = time.time(), time.time()
    con = db()
    known = {row[0]: (row[1], row[2]) for row in con.execute("SELECT path, size, mtime FROM files")}
    count = changed = 0
    for dirpath, dirnames, filenames in os.walk(ROOT):
        rel_dir = os.path.relpath(dirpath, ROOT)
        rel_dir = "" if rel_dir == "." else rel_dir
        dirnames[:] = [d for d in dirnames if not excluded(f"{rel_dir}/{d}".lstrip("/"))]
        for fn in filenames:
            rel = f"{rel_dir}/{fn}".lstrip("/")
            if excluded(rel):
                continue
            p = pathlib.Path(dirpath) / fn
            try:
                st = p.stat()
            except OSError:
                continue
            count += 1
            if known.get(rel) == (st.st_size, st.st_mtime):
                con.execute("UPDATE files SET seen=? WHERE path=?", (now, rel))
                continue
            body = extract(p) if p.suffix.lower() in TEXT_EXT | DOC_EXT else ""
            con.execute("DELETE FROM fts WHERE path=?", (rel,))
            con.execute("INSERT INTO fts(path, name, body) VALUES (?,?,?)", (rel, fn.replace("_", " ").replace("-", " "), body))
            con.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?)", (rel, st.st_size, st.st_mtime, now))
            changed += 1
            if changed % 500 == 0:
                con.commit()
    gone = [r[0] for r in con.execute("SELECT path FROM files WHERE seen < ?", (now,))]
    for rel in gone:
        con.execute("DELETE FROM files WHERE path=?", (rel,))
        con.execute("DELETE FROM fts WHERE path=?", (rel,))
    con.commit()
    con.close()
    log.info("index: %d files, %d (re)indexed, %d removed in %.0fs", count, changed, len(gone), time.time() - t0)
    return count


async def run_index():
    if state["running"]:
        return
    state["running"] = True
    try:
        state["last_count"] = await asyncio.get_running_loop().run_in_executor(None, reindex)
        state["last_run"] = time.strftime("%Y-%m-%d %H:%M")
    except Exception:
        log.exception("reindex failed")
    finally:
        state["running"] = False


async def nightly():
    while True:
        lt = time.localtime()
        secs = ((REINDEX_HOUR - lt.tm_hour) % 24) * 3600 - lt.tm_min * 60 - lt.tm_sec
        await asyncio.sleep(secs if secs > 0 else secs + 86400)
        await run_index()


@web.middleware
async def auth(req, handler):
    if req.headers.get("Authorization", "") != f"Bearer {TOKEN}":
        return web.json_response({"error": "unauthorized"}, status=401)
    return await handler(req)


def fts_query(q: str) -> str:
    words = [w for w in "".join(c if c.isalnum() else " " for c in q).split() if len(w) > 1]
    return " ".join(f'"{w}"*' for w in words[:12])


async def h_search(req):
    q, limit = req.query.get("q", ""), min(int(req.query.get("limit", 8)), 25)
    fq = fts_query(q)
    if not fq:
        return web.json_response({"error": "empty query"}, status=400)
    con = db()
    rows = con.execute(
        "SELECT fts.path, snippet(fts, 2, '[', ']', ' … ', 18), files.size, files.mtime "
        "FROM fts JOIN files ON files.path = fts.path WHERE fts MATCH ? ORDER BY bm25(fts, 0, 4.0, 1.0) LIMIT ?",
        (fq, limit)).fetchall()
    con.close()
    return web.json_response({"query": q, "results": [
        {"path": "/volume1/" + r[0], "snippet": r[1], "size_kb": round(r[2] / 1024),
         "modified": time.strftime("%Y-%m-%d", time.localtime(r[3]))} for r in rows],
        "indexing": state["running"]})


async def h_find(req):
    name, limit = req.query.get("name", "").strip().lower(), min(int(req.query.get("limit", 20)), 50)
    if not name:
        return web.json_response({"error": "empty name"}, status=400)
    con = db()
    rows = con.execute("SELECT path, size, mtime FROM files WHERE lower(path) LIKE ? ORDER BY mtime DESC LIMIT ?",
                       (f"%{name}%", limit)).fetchall()
    con.close()
    return web.json_response({"results": [{"path": "/volume1/" + r[0], "size_kb": round(r[1] / 1024),
                                           "modified": time.strftime("%Y-%m-%d", time.localtime(r[2]))} for r in rows]})


async def h_read(req):
    raw = req.query.get("path", "").strip()
    rel = raw[len("/volume1/"):] if raw.startswith("/volume1/") else raw.lstrip("/")
    p = (ROOT / rel).resolve()
    if ROOT not in p.parents or excluded(str(p.relative_to(ROOT))):
        return web.json_response({"error": "that path is not allowed"}, status=403)
    if not p.is_file():
        return web.json_response({"error": "no such file"}, status=404)
    if p.suffix.lower() not in TEXT_EXT | DOC_EXT:
        st = p.stat()
        return web.json_response({"path": raw, "note": "not a text document; only metadata available",
                                  "size_kb": round(st.st_size / 1024),
                                  "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))})
    text = await asyncio.get_running_loop().run_in_executor(None, extract, p)
    off = max(0, int(req.query.get("offset", 0)))
    return web.json_response({"path": raw, "offset": off, "total_chars": len(text),
                              "text": text[off:off + 8000], "more": off + 8000 < len(text)})


async def h_stats(req):
    con = db()
    n = con.execute("SELECT count(*) FROM files").fetchone()[0]
    con.close()
    return web.json_response({"files_indexed": n, "last_run": state["last_run"], "indexing": state["running"]})


async def on_startup(app):
    pathlib.Path(DB).parent.mkdir(parents=True, exist_ok=True)
    db().close()
    app["tasks"] = [asyncio.create_task(run_index()), asyncio.create_task(nightly())]


app = web.Application(middlewares=[auth])
app.router.add_get("/search", h_search)
app.router.add_get("/find", h_find)
app.router.add_get("/read", h_read)
app.router.add_get("/stats", h_stats)
app.on_startup.append(on_startup)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=8095)
