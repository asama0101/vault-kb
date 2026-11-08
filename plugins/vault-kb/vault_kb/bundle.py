"""OKF の束（vault の根）のモデル。ノート・走査・索引・ログ。"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from vault_kb import conflict
from vault_kb.vault import (
    ARCHIVE_NAME,
    INBOX_NAME,
    KNOWLEDGE_NAME,
    KNOWLEDGE_PROJECT,
    NOTES_NAME,
    OKF_VERSION,
    PROJECT_ROOTS,
    RESERVED_FOLDED,
    SHARED_NAME,
    SHARED_PROJECT,
    Vault,
    atomic_write,
    category_sort_key,
    fold_name,
    is_ignored,
    is_link,
    is_supported,
    walk,
)

NOTE_KEYS = (
    "type",
    "title",
    "description",
    "resource",
    "tags",
    "timestamp",
    "project",
    "category",
    "client",
    "doc_type",
    "date",
    "sha256",
    "size",
    "mtime_ns",
    "readable",
)
_HEAD_BYTES = 64 * 1024
_KEY_LINE = re.compile(r"([^:\s][^:]*):\s*(.*)")
_SHA256 = re.compile(r"[0-9a-f]{64}")
LOG_KEEP_DAYS = 90  # log.md に残す日数（これより古い日付の見出しは消す）
_LOG_HEADING = re.compile(r"## (\d{4}-\d{2}-\d{2})")
_NOTE_CACHE: dict[Path, tuple[tuple[int, int], bool]] = {}  # 見張りが毎回開かないための記憶
_PROJECT_ROOTS_FOLDED = {fold_name(name) for name in PROJECT_ROOTS}


@dataclass(frozen=True)
class Note:
    path: Path
    meta: dict[str, Any]
    original: Path


def _parse_value(raw: str) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            return raw[1:-1]
        return raw


def _head_lines(path: Path) -> list[str] | None:
    """先頭の frontmatter の行（なければ None）。閉じ線が見つかるまで読む範囲を広げる。"""
    size = _HEAD_BYTES
    while True:
        with path.open("rb") as file:
            data = file.read(size)
        lines = data.decode("utf-8", errors="replace").replace("\r\n", "\n").split("\n")
        if lines[0] != "---":
            return None
        complete = len(data) < size
        for number, line in enumerate(lines[1:], start=1):
            if line == "---" and (complete or number < len(lines) - 1):
                return lines[1:number]
        if complete:
            return None
        size *= 2


def read_frontmatter(path: Path) -> dict[str, Any]:
    """ノートの frontmatter。JSON の値と、Obsidian が書き直した形の両方を読む。"""
    meta: dict[str, Any] = {}
    key = None
    for line in _head_lines(path) or []:
        item = line.strip()
        if item.startswith("-") and key is not None and (item == "-" or item[1] == " "):
            if not isinstance(meta[key], list):
                meta[key] = []
            meta[key].append(_parse_value(item[1:].strip()))
        elif match := _KEY_LINE.fullmatch(line):
            key = match[1].strip()
            meta[key] = _parse_value(match[2].strip())
    return meta


def note_text(meta: dict[str, Any], body: str) -> str:
    """ノートの全文。frontmatter は NOTE_KEYS の順、続けて足された鍵を `key: <json>` で書く。"""
    keys = [k for k in NOTE_KEYS if k in meta] + [k for k in meta if k not in NOTE_KEYS]
    head = "".join(f"{key}: {json.dumps(meta[key], ensure_ascii=False)}\n" for key in keys)
    return f"---\n{head}---\n\n{body.strip()}\n"


def write_note(path: Path, meta: dict[str, Any], body: str) -> None:
    atomic_write(path, note_text(meta, body))


def location(vault: Vault, path: Path) -> tuple[str, str] | None:
    """区分より下のファイルなら (案件, 区分)。案件フォルダ・30_shared・40_knowledge の下だけ。"""
    try:
        parts = path.relative_to(vault.root).parts
    except ValueError:
        return None
    if parts[:1] and parts[0] in PROJECT_ROOTS and len(parts) >= 4:
        return parts[1], parts[2]
    if parts[:1] == (SHARED_NAME,) and len(parts) >= 3:
        return SHARED_PROJECT, parts[1]
    if parts[:1] == (KNOWLEDGE_NAME,) and len(parts) >= 3:
        return KNOWLEDGE_PROJECT, parts[1]
    return None


def _shallow(folder: Path) -> tuple[list[Path], list[Path]]:
    """folder 直下の (フォルダ, ファイル)。リンクは含めない。"""
    entries = [e for e in folder.iterdir() if not is_link(e)]
    return [e for e in entries if e.is_dir()], [e for e in entries if e.is_file()]


def projects(vault: Vault) -> list[str]:
    """案件名（名前順。20_projects・80_archive の案件と shared・knowledge。リンクは除く）。"""
    return sorted({name for name, _ in vault.project_folders()})


def _categories_in(folder: Path) -> list[str]:
    folders, _ = _shallow(folder)
    return sorted((f.name for f in folders if not f.name.startswith(".")), key=category_sort_key)


def categories(vault: Vault, project: str) -> list[str]:
    folder = vault.project_dir(project)
    return _categories_in(folder) if folder else []


def _project_files(vault: Vault) -> list[tuple[str, Path]]:
    found = []
    for project, folder in vault.project_folders():
        _, files = walk(folder)
        found += [(project, f) for f in sorted(files)]
    return found


def _is_resource(value: Any) -> bool:
    """resource として使えるか（base からの相対パス。絶対パス・.. ・\・: は不可）。"""
    return (
        isinstance(value, str)
        and not any(c in value for c in "\\:")
        and not {"", ".", ".."} & set(value.split("/"))  # 空の部分は、空文字・先頭の /・// を含む
    )


def _date(meta: dict[str, Any]) -> str:
    """日付の文字列。文字列でない値・空は "" （日付なし）。"""
    value = meta.get("date")
    return value if isinstance(value, str) else ""


def is_note(meta: dict[str, Any]) -> bool:
    """片づけが書いたノートの形か（type・相対パスの resource・指紋の型）。違えば利用者のメモ。"""
    sha = meta.get("sha256")
    return (
        meta.get("type") == "document"
        and _is_resource(meta.get("resource"))
        and isinstance(sha, str)
        and _SHA256.fullmatch(sha) is not None
        and all(type(meta.get(key)) is int for key in ("size", "mtime_ns"))  # bool を除く
    )


def notes(vault: Vault) -> list[Note]:
    """notes_dir の下のノート（原本の有無は問わない）。"""
    if not vault.notes_dir.is_dir():
        return []
    found = []
    _, files = walk(vault.notes_dir)
    for path in sorted(files):
        if path.suffix.lower() != ".md" or fold_name(path.name) in RESERVED_FOLDED:
            continue
        meta = read_frontmatter(path)
        if is_note(meta):
            found.append(Note(path, meta, vault.root / meta["resource"]))
    return found


def is_original_file(path: Path) -> bool:
    """資料として扱うファイルか。無視する名前と、片づけが書いたノート（.md）は資料ではない。"""
    if is_ignored(path.name):
        return False
    if path.suffix.lower() != ".md":
        return True
    try:
        info = path.stat()
        stamp = (info.st_size, info.st_mtime_ns)
        if (cached := _NOTE_CACHE.get(path)) and cached[0] == stamp:
            return not cached[1]
        note = is_note(read_frontmatter(path))
    except OSError:
        return False  # 開けない .md（使用中など）は、ノートかもしれない。次の機会に判定する
    _NOTE_CACHE[path] = (stamp, note)
    return not note


def candidates(vault: Vault) -> list[Path]:
    """区分より下の、無視しないファイル（拡張子を問わない照合先）。. 始まりのフォルダの中は除く。"""
    return [
        path
        for _, path in _project_files(vault)
        if location(vault, path)
        and not any(part.startswith(".") for part in path.relative_to(vault.root).parts)
        and is_original_file(path)
    ]


def originals(vault: Vault) -> list[Path]:
    return [path for path in candidates(vault) if is_supported(path.name)]


def inbox_files(vault: Vault) -> list[Path]:
    """00_inbox 直下のファイル（拡張子は問わない。無視する名前を除く）。"""
    inbox = vault.root / INBOX_NAME
    if not inbox.is_dir() or is_link(inbox):
        return []
    _, files = _shallow(inbox)
    held = conflict.held_sources(vault)
    return sorted(f for f in files if is_original_file(f) and f not in held)


def project_root_files(vault: Vault) -> list[tuple[str, Path]]:
    """案件フォルダ直下のファイル（無視する名前を除く）。"""
    found = []
    held = conflict.held_sources(vault)
    for project, folder in vault.project_folders():
        _, files = _shallow(folder)
        found += [(project, f) for f in sorted(files) if is_original_file(f) and f not in held]
    return found


def clients(all_notes: list[Note]) -> list[str]:
    """ノートにある取引先の表記（空でないもの。初出順、重複なし）。"""
    names = (n.meta.get("client") for n in all_notes)
    return list(dict.fromkeys(c for c in names if isinstance(c, str) and c))


def _fold_client(name: str) -> str:
    return "".join(unicodedata.normalize("NFKC", name).casefold().split())


def canonical_client(existing: list[str], raw: str) -> str:
    """全角半角・大文字小文字・空白の違いを畳み、既存の表記に寄せる。"""
    key = _fold_client(raw)
    return next((name for name in existing if _fold_client(name) == key), raw.strip())


def link(target_rel: str, label: str) -> str:
    """Markdown のリンク。`<>` で空白を許し、`>` と改行は % エンコードする。

    ラベルは、改行を空白にし、円記号と角括弧をエスケープする。
    """
    target = target_rel.replace(">", "%3E").replace("\n", "%0A")
    escaped = (
        label.replace("\r", " ")
        .replace("\n", " ")
        .replace("\\", "\\\\")
        .replace("[", r"\[")
        .replace("]", r"\]")
    )
    return f"[{escaped}](<{target}>)"


def _relative(path: Path, folder: Path) -> str:
    """folder から path への相対パス（.. をたどる。Markdown のリンク用に / 区切り）。"""
    return Path(os.path.relpath(path, folder)).as_posix()


def _note_line(note: Note, folder: Path) -> str:
    meta = note.meta
    title = str(meta.get("title") or note.path.stem)
    description = " ".join(str(meta.get("description") or "").split())
    original = note.original
    date = _date(meta) or "日付なし"
    line = (
        f"- {link(_relative(note.path, folder), title)} — {description}"
        f"（原本: {link(_relative(original, folder), original.name)}、{date}）"
    )
    return line + ("" if meta.get("readable") is not False else "（文字を取り出せていない）")


def _sorted_notes(found: list[Note]) -> list[Note]:
    by_title = sorted(found, key=lambda n: str(n.meta.get("title") or ""))
    return sorted(by_title, key=lambda n: _date(n.meta), reverse=True)


def _prune_indexes(vault: Vault) -> None:
    """対応するフォルダがなくなった notes_dir の index.md と、空になったフォルダを消す。"""
    for here, _, names in os.walk(vault.notes_dir, topdown=False):
        folder = Path(here)
        if (vault.root / folder.relative_to(vault.notes_dir)).is_dir():
            continue
        if "index.md" in names:
            (folder / "index.md").unlink()
        if not any(folder.iterdir()):
            folder.rmdir()


def _write_index(path: Path, text: str) -> None:
    """索引を書く。中身が同じなら書かない（更新日時を変えず、同期の通信を増やさない）。"""
    try:
        if path.read_bytes() == text.encode("utf-8"):
            return
    except OSError:
        pass
    atomic_write(path, text)


def _group_notes(
    vault: Vault, all_notes: list[Note]
) -> tuple[dict[Path, list[Note]], dict[Path, list[Note]]]:
    """ノートを、原本のある案件フォルダと、区分のフォルダ（サブフォルダの中も含む）ごとに分ける。

    案件ごとに全ノートを調べ直さないため（案件数 × ノート数の時間になる）、1 回の走査で分ける。
    """
    by_project: dict[Path, list[Note]] = {}
    by_category: dict[Path, list[Note]] = {}
    for note in all_notes:
        parts = note.original.relative_to(vault.root).parts
        # 20_projects/<案件>、30_shared など。根の名前は Windows と同じく大文字小文字を区別しない
        depth = 2 if fold_name(parts[0]) in _PROJECT_ROOTS_FOLDED else 1
        folder = vault.root.joinpath(*parts[:depth])
        by_project.setdefault(folder, []).append(note)
        if len(parts) > depth + 1:
            by_category.setdefault(folder / parts[depth], []).append(note)
    return by_project, by_category


def write_indexes(vault: Vault) -> None:
    """根の index.md と、notes_dir の案件・区分の index.md を作り直す。"""
    vault.hide_notes_dir()
    _prune_indexes(vault)
    by_project, by_category = _group_notes(vault, notes(vault))
    root_lines = []
    for project, folder in vault.project_folders():
        relative = folder.relative_to(vault.root).as_posix()
        notes_folder = vault.notes_dir / relative
        label = project + ("（アーカイブ）" if relative.startswith(f"{ARCHIVE_NAME}/") else "")
        in_project = by_project.get(folder, [])
        root_lines.append(
            f"- {link(f'{NOTES_NAME}/{relative}/index.md', label)} — {len(in_project)} 件\n"
        )
        project_lines = []
        for category in _categories_in(folder):
            cat_folder = notes_folder / category
            in_category = by_category.get(folder / category, [])
            project_lines.append(
                f"- {link(f'{category}/index.md', category)} — {len(in_category)} 件\n"
            )
            lines = "".join(f"{_note_line(n, cat_folder)}\n" for n in _sorted_notes(in_category))
            _write_index(cat_folder / "index.md", f"# {project} / {category}\n\n{lines}")
        _write_index(notes_folder / "index.md", f"# {project}\n\n{''.join(project_lines)}")
    head = f'---\nokf_version: "{OKF_VERSION}"\n---\n# vault-kb\n\n'
    _write_index(vault.root / "index.md", head + "".join(root_lines))


def _prune_log(text: str, today: datetime) -> str:
    """LOG_KEEP_DAYS 日より古い日付の見出しと、その下の行を消す（見出しの前の文章は残す）。"""
    cutoff = (today - timedelta(days=LOG_KEEP_DAYS)).date().isoformat()
    kept = []
    old = False
    for line in text.splitlines(keepends=True):
        if line.startswith("#"):  # 見出し。日付の見出しだけが、古ければ消える（ほかの見出しは残す）
            heading = _LOG_HEADING.fullmatch(line.rstrip("\r\n"))
            old = bool(heading) and heading[1] < cutoff
        if not old:
            kept.append(line)
    pruned = "".join(kept)
    return pruned.rstrip("\n") + "\n" if pruned.strip() else ""


def append_log(vault: Vault, entries: list[str], now: datetime) -> None:
    """log.md の今日の見出しの直後に entries を足す（見出しがなければ先頭に作る）。

    LOG_KEEP_DAYS 日より古い日付の見出しは、このとき消す。
    """
    path = vault.root / "log.md"
    existing = _prune_log(path.read_text(encoding="utf-8"), now) if path.exists() else ""
    heading = f"## {now.date().isoformat()}\n"
    lines = "".join(f"- {entry}\n" for entry in entries)
    if existing.startswith(heading):
        rest = existing[len(heading) :].removeprefix("\n")
        text = f"{heading}\n{lines}{rest}"
    else:
        text = f"{heading}\n{lines}" + (f"\n{existing}" if existing else "")
    atomic_write(path, text)
