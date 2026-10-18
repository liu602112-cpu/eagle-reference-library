"""Read-only Eagle source, local SQLite reference index, and a browsable contact sheet."""
from __future__ import annotations

import argparse
import os
import colorsys
import hashlib
import html
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_LIBRARY = Path(os.environ.get("EAGLE_LIBRARY", str(Path.home() / "Pictures/Eagle.library")))
DEFAULT_DATA = Path(os.environ.get("EAGLE_INDEX", str(Path(__file__).resolve().parents[1] / "data")))
EXTENSIONS = {"jpg", "jpeg", "png", "webp", "gif", "avif", "bmp", "tif", "tiff", "svg", "heic", "heif"}
PLATFORMS = {"xiaohongshu": "小红书", "wechat": "公众号", "douyin": "抖音"}


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def dump(obj):
    return json.dumps(obj, ensure_ascii=False)


def open_db(data_root):
    data_root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(data_root / "references.sqlite3")
    db.row_factory = sqlite3.Row
    db.executescript("""
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS folders(id TEXT PRIMARY KEY, name TEXT, path TEXT, description TEXT, tags_json TEXT);
    CREATE TABLE IF NOT EXISTS items(
      id TEXT PRIMARY KEY, name TEXT, ext TEXT, original TEXT, thumbnail TEXT,
      width INTEGER, height INTEGER, size INTEGER, sha256 TEXT, file_mtime_ns INTEGER,
      folders_json TEXT, folder_ids_json TEXT, tags_json TEXT, annotation TEXT, source_url TEXT,
      palettes_json TEXT, derived_tags_json TEXT, search_text TEXT,
      deleted INTEGER, available INTEGER, metadata_modified INTEGER, updated_at TEXT);
    CREATE INDEX IF NOT EXISTS items_status ON items(deleted, available);
    CREATE INDEX IF NOT EXISTS items_hash ON items(sha256);
    CREATE TABLE IF NOT EXISTS visual_notes(
      id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, tags_json TEXT NOT NULL, note TEXT NOT NULL,
      reviewed_at TEXT NOT NULL);
    """)
    return db


def walk_folders(nodes, prefix=""):
    result = []
    for folder in nodes:
        name = str(folder.get("name", ""))
        path = prefix + "/" + name if prefix else name
        result.append({"id": str(folder["id"]), "name": name, "path": path,
                       "description": str(folder.get("description", "")), "tags": folder.get("tags", [])})
        result.extend(walk_folders(folder.get("children", []), path))
    return result


def safe_file(directory, filename):
    candidate = directory / filename
    try:
        if candidate.resolve().parent == directory.resolve() and candidate.is_file():
            return candidate
    except (OSError, ValueError):
        pass
    return None


def locate_image(directory, metadata):
    ext = str(metadata.get("ext", "")).lower()
    original = safe_file(directory, str(metadata.get("name", "")) + "." + ext)
    if original is None:
        candidates = [p for p in directory.iterdir() if p.is_file()
                      and p.suffix.lower().lstrip(".") == ext and not p.stem.endswith("_thumbnail")]
        original = candidates[0] if len(candidates) == 1 else None
    thumbnail = safe_file(directory, str(metadata.get("name", "")) + "_thumbnail.png")
    if thumbnail is None:
        thumbnail = next(iter(directory.glob("*_thumbnail.png")), None)
    return original, thumbnail or original


def derive_tags(metadata, folders):
    width, height = int(metadata.get("width", 0)), int(metadata.get("height", 0))
    result = []
    if width and height:
        ratio = height / width
        result += ["长图" if ratio >= 2.5 else "竖图" if ratio > 1.15 else "横图" if ratio < .87 else "方图"]
    for p in metadata.get("palettes", [])[:6]:
        color = p.get("color", [])
        if len(color) != 3 or float(p.get("ratio", 0)) < 2:
            continue
        r, g, b = [max(0, min(255, float(x))) / 255 for x in color]
        hue, saturation, value = colorsys.rgb_to_hsv(r, g, b)
        if value < .24:
            label = "黑色 暗色"
        elif saturation < .12:
            label = "白色 浅色" if value > .88 else "灰色"
        else:
            h = hue * 360
            label = ("红色" if h < 18 or h >= 345 else "橙色 棕色" if h < 45 else
                     "黄色 米色" if h < 72 else "绿色" if h < 165 else "青色" if h < 195 else
                     "蓝色" if h < 265 else "紫色" if h < 305 else "粉色")
            if saturation < .30:
                result.append("低饱和")
        result.extend(label.split())
    folder_text = " ".join(folders).lower()
    if "texture" in folder_text or "纹理" in folder_text:
        result.extend(["纹理", "材质", "背景"])
    if "排版" in folder_text:
        result.append("排版")
    if "封面" in folder_text:
        result.append("封面")
    return sorted(set(result))


def sync(library, data_root):
    library = library.resolve(strict=True)
    if not (library / "images").is_dir():
        raise ValueError("Eagle library images directory is unavailable; the previous index was preserved.")
    # Validate the root before opening/writing our index. Never write into Eagle.
    root_metadata = read_json(library / "metadata.json")
    folders = walk_folders(root_metadata.get("folders", []))
    folder_map = {f["id"]: f for f in folders}
    db = open_db(data_root)
    old_library = db.execute("SELECT value FROM settings WHERE key='library'").fetchone()
    if old_library and Path(old_library[0]).resolve() != library:
        raise ValueError("Use a separate data directory for each Eagle library.")
    existing = {r["id"]: dict(r) for r in db.execute("SELECT * FROM items")}
    current = []
    errors = []
    for directory in sorted((library / "images").glob("*.info")):
        metadata_path = directory / "metadata.json"
        try:
            metadata = read_json(metadata_path)
            item_id = str(metadata["id"])
            if item_id != directory.name[:-5]:
                raise ValueError("Item ID does not match its Eagle directory")
            ext = str(metadata.get("ext", "")).lower()
            if ext not in EXTENSIONS:
                continue
            deleted = bool(metadata.get("isDeleted", False))
            original, thumbnail = locate_image(directory, metadata)
            old = existing.get(item_id, {})
            size = original.stat().st_size if original else 0
            mtime = original.stat().st_mtime_ns if original else 0
            checksum = ""
            if original and not deleted:
                if old.get("file_mtime_ns") == mtime and old.get("size") == size and old.get("sha256"):
                    checksum = old["sha256"]
                else:
                    with original.open("rb") as handle:
                        checksum = hashlib.file_digest(handle, "sha256").hexdigest()
            folder_ids = [str(f) for f in metadata.get("folders", [])]
            paths = [folder_map[f]["path"] if f in folder_map else "未找到分类/" + f for f in folder_ids]
            tags = [str(t) for t in metadata.get("tags", [])]
            derived = derive_tags(metadata, paths)
            annotation = str(metadata.get("annotation", ""))
            fields = [str(metadata.get("name", "")), annotation, *paths, *tags, *derived]
            for f in folder_ids:
                if f in folder_map:
                    fields += [folder_map[f]["description"], *folder_map[f]["tags"]]
            current.append((item_id, str(metadata.get("name", "")), ext, str(original or ""),
                            str(thumbnail or ""), int(metadata.get("width", 0)), int(metadata.get("height", 0)),
                            size, checksum, mtime, dump(paths), dump(folder_ids), dump(tags), annotation,
                            str(metadata.get("url", "")), dump(metadata.get("palettes", [])), dump(derived),
                            " ".join(fields).casefold(), int(deleted), int(original is not None),
                            int(metadata.get("modificationTime", 0)), stamp()))
        except (OSError, ValueError, KeyError, TypeError) as error:
            errors.append({"id": directory.name[:-5], "error": str(error)})
    with db:
        db.execute("DELETE FROM folders")
        db.executemany("INSERT INTO folders VALUES(?,?,?,?,?)", [(f["id"], f["name"], f["path"], f["description"], dump(f["tags"])) for f in folders])
        # A removed or currently unreadable source must not remain selectable.
        db.execute("UPDATE items SET available=0")
        db.executemany("INSERT OR REPLACE INTO items VALUES(" + ",".join("?" for _ in range(22)) + ")", current)
        db.executemany("INSERT OR REPLACE INTO settings VALUES(?,?)", [("library", str(library)), ("last_sync", stamp()), ("sync_errors", dump(errors))])
    report = statistics(db)
    db.close()
    (data_root / "sync-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def statistics(db):
    settings = {r["key"]: r["value"] for r in db.execute("SELECT * FROM settings")}
    items = list(db.execute("SELECT * FROM items WHERE available=1 AND deleted=0"))
    reviewed = db.execute("SELECT COUNT(*) FROM items i JOIN visual_notes v ON i.id=v.id AND i.sha256=v.sha256 WHERE i.deleted=0 AND i.available=1").fetchone()[0]
    folder_counts = []
    for f in db.execute("SELECT * FROM folders ORDER BY path"):
        count = sum(f["id"] in json.loads(i["folder_ids_json"]) for i in items)
        folder_counts.append({"id": f["id"], "path": f["path"], "count": count})
    hashes = [i["sha256"] for i in items if i["sha256"]]
    return {"library": settings.get("library"), "last_sync": settings.get("last_sync"), "active_images": len(items),
            "deleted_images": db.execute("SELECT COUNT(*) FROM items WHERE deleted=1 AND available=1").fetchone()[0],
            "unavailable_images": db.execute("SELECT COUNT(*) FROM items WHERE available=0").fetchone()[0],
            "unique_images": len(set(hashes)), "duplicate_records": len(hashes) - len(set(hashes)),
            "visually_reviewed": reviewed, "unfiled": sum(not json.loads(i["folder_ids_json"]) for i in items),
            "folders": folder_counts, "errors": json.loads(settings.get("sync_errors", "[]"))}


def get_items(db):
    rows = db.execute("""SELECT i.*, v.tags_json AS visual_tags, v.note AS visual_note, v.reviewed_at
                         FROM items i LEFT JOIN visual_notes v ON i.id=v.id AND i.sha256=v.sha256
                         WHERE i.deleted=0 AND i.available=1""")
    items = []
    for row in rows:
        r = dict(row)
        for source, dest in [("folders_json", "folders"), ("tags_json", "tags"), ("palettes_json", "palettes"),
                             ("derived_tags_json", "derived_tags"), ("visual_tags", "visual_tags")]:
            r[dest] = json.loads(r.get(source) or "[]")
            if source != dest:
                del r[source]
        r["search_text"] += " " + " ".join(r["visual_tags"]) + " " + (r["visual_note"] or "")
        r["original_uri"] = Path(r["original"]).as_uri()
        r["thumbnail_uri"] = Path(r["thumbnail"]).as_uri()
        r["eagle_uri"] = "eagle://item/" + r["id"]
        items.append(r)
    return items


def query_items(items, query="", folder="", platform="", limit=12, color=""):
    terms = [t.casefold() for t in re.split(r"[\s,，/]+", query) if t]
    platform = PLATFORMS.get(platform, platform)
    results = []
    for item in items:
        folders = " ".join(item["folders"])
        if folder and folder.casefold() not in folders.casefold():
            continue
        if color and color.casefold() not in " ".join(item["derived_tags"] + item["visual_tags"]).casefold():
            continue
        hits = [t for t in terms if t in item["search_text"].casefold()]
        if terms and not hits:
            continue
        platform_match = bool(platform and platform in (folders + " " + " ".join(item["visual_tags"])))
        score = len(hits) * 10 + (20 if platform_match else 0) + (2 if item["visual_note"] else 0)
        item = dict(item, relevance=score, matched_terms=hits, platform_match=platform_match)
        results.append(item)
    results.sort(key=lambda r: (-r["relevance"], r["id"]))
    # Byte-identical references count as one candidate, with all original records retained in the DB.
    unique = []
    seen = set()
    for item in results:
        key = item["sha256"] or item["id"]
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique[:limit] if limit else unique


def annotate(db, path):
    records = read_json(path)
    if not isinstance(records, list):
        raise ValueError("Annotations must be a JSON array based on actually inspected images.")
    updated = []
    with db:
        for record in records:
            row = db.execute("SELECT sha256 FROM items WHERE id=? AND available=1 AND deleted=0", (record["id"],)).fetchone()
            if not row:
                raise ValueError("Image unavailable: " + str(record["id"]))
            if record.get("sha256") and record["sha256"] != row[0]:
                raise ValueError("Image changed since review: " + record["id"])
            previous = db.execute("SELECT tags_json,note FROM visual_notes WHERE id=? AND sha256=?", (record["id"], row[0])).fetchone()
            tags = sorted(set((json.loads(previous[0]) if previous else []) + record.get("tags", [])))
            note = str(record.get("note", "")).strip()
            if not note:
                raise ValueError("A visual note is required.")
            if previous and note not in previous[1]:
                note = previous[1] + "\n" + note
            db.execute("INSERT OR REPLACE INTO visual_notes VALUES(?,?,?,?,?)", (record["id"], row[0], dump(tags), note, stamp()))
            updated.append(record["id"])
    return {"annotated": updated}


def make_gallery(items, report, output):
    data = json.dumps(items, ensure_ascii=False).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    template = Path(__file__).with_name("gallery.html").read_text(encoding="utf-8")
    document = template.replace("__ITEM_DATA__", data).replace("__SYNC_TIME__", html.escape(report["last_sync"] or ""))
    document = document.replace("__LIBRARY__", html.escape(report["library"] or ""))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(document, encoding="utf-8")
    return {"gallery": str(output), "images": len(items)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, default=DEFAULT_LIBRARY)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("sync")
    commands.add_parser("stats")
    for name in ("search", "gallery"):
        child = commands.add_parser(name)
        child.add_argument("--query", default="")
        child.add_argument("--folder", default="")
        child.add_argument("--platform", default="")
        child.add_argument("--color", default="")
        child.add_argument("--limit", type=int, default=12 if name == "search" else 0)
        if name == "gallery":
            child.add_argument("--output", type=Path)
    child = commands.add_parser("annotate")
    child.add_argument("--file", type=Path, required=True)
    args = parser.parse_args()
    report = sync(args.library, args.data_root)
    if args.command in ("sync", "stats"):
        return report
    db = open_db(args.data_root)
    try:
        if args.command == "annotate":
            return annotate(db, args.file)
        items = query_items(get_items(db), args.query, args.folder, args.platform, args.limit, args.color)
        if args.command == "gallery":
            return make_gallery(items, report, args.output or args.data_root / "index.html")
        return {"last_sync": report["last_sync"], "source": report["library"], "sync_errors": report["errors"],
                "matches": len(items), "items": [{k: item[k] for k in (
                    "id", "name", "original", "thumbnail", "width", "height", "sha256", "folders", "tags",
                    "derived_tags", "visual_tags", "visual_note", "reviewed_at", "source_url", "relevance", "matched_terms", "platform_match")}
                    for item in items]}
    finally:
        db.close()


if __name__ == "__main__":
    try:
        # ASCII JSON stays lossless across Windows PowerShell code pages.
        print(json.dumps(main(), ensure_ascii=True))
    except Exception as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=True))
        sys.exit(1)
