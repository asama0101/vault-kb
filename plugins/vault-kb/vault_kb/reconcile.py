"""片づけの準備。機械的に直せるものを直し、AI の判断が要るものを変換して返す。"""

from __future__ import annotations

import os
import shutil
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from . import bundle, conflict, convert
from .vault import (
    ARCHIVE_NAME,
    DEFAULT_CATEGORY,
    INBOX_NAME,
    KNOWLEDGE_NAME,
    KNOWLEDGE_PROJECT,
    MAX_CATEGORY_NUMBER,
    PROJECT_ROOTS,
    PROJECTS_NAME,
    SHARED_NAME,
    SHARED_PROJECT,
    Vault,
    atomic_write,
    check_folder_name,
    fold_name,
    is_ignored,
    is_link,
    is_supported,
    move_file,
    next_category_name,
    resolve_category,
    split_category,
)

LOCK_STALE_SECONDS = 900
MAX_PATH_LEN = 240
EXCERPT_CHARS = 4000  # Claude が判断に使う本文の長さ（staging の Read には確認が出る）
MAX_ITEMS = 20  # 1 回の prepare が返す項目の上限（抜粋が多すぎると Claude が受け取れない）
MAX_ABS_PATH = 259  # Windows の MAX_PATH（260）から終端を除いた長さ
PLAN_NAME = ".vault-kb-plan.json"  # vault の直下。. 始まりなので走査が無視する
_PREVIOUS_KEYS = ("title", "description", "tags", "client", "doc_type", "date")
_NOT_CONVERTED = "変換結果がありません（原本が変わった可能性）。次回に回します"

Item = dict[str, Any]
_RESERVED_PROJECTS = {SHARED_PROJECT: SHARED_NAME, KNOWLEDGE_PROJECT: KNOWLEDGE_NAME}


class Busy(Exception):
    """別の片づけが進行中（ロックが 15 分以内）。"""


def _lock_path(data_dir: Path) -> Path:
    return data_dir / "reconcile.lock"


def _create_lock(path: Path, now: datetime) -> None:
    """ロックを新しく作る（あれば FileExistsError）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(handle, "w", encoding="utf-8") as file:
        file.write(now.isoformat())


def _is_fresh(path: Path, now: datetime) -> bool:
    try:
        written = datetime.fromisoformat(path.read_text(encoding="utf-8").strip())
        # 未来の時刻（時計が戻った）を生きているとみなすと、いつまでも取れない
        return 0 <= (now - written).total_seconds() < LOCK_STALE_SECONDS
    except (OSError, ValueError, TypeError):
        return False


def acquire(data_dir: Path, now: datetime) -> None:
    """ロックを取る。進行中（15 分未満）なら Busy。古い・読めないロックは消して取り直す。"""
    path = _lock_path(data_dir)
    # shortcut: 古いロックの引き継ぎは、2 本が同時に「古い」と見ると両方が取れる。
    # 落ちたロックが残った直後に、ask と見張りが同時に動くときだけ。同名の保留があるので壊れはしない
    busy = Busy("別の片づけが進行中です。15 分後にもう一度試してください。")
    try:
        _create_lock(path, now)
    except FileExistsError:
        if _is_fresh(path, now):
            raise busy from None
        path.unlink(missing_ok=True)
        try:
            _create_lock(path, now)
        except FileExistsError:
            raise busy from None


def release(data_dir: Path) -> None:
    _lock_path(data_dir).unlink(missing_ok=True)


def staging_path(data_dir: Path, sha: str) -> Path:
    return data_dir / "staging" / f"{sha}.md"


def _unreadable_path(data_dir: Path, sha: str) -> Path:
    """読めなかった原本の印（中身は理由）。"""
    return data_dir / "staging" / f"{sha}.unreadable"


def _converted(data_dir: Path, sha: str) -> tuple[str, str | None]:
    """prepare が書いた (本文, 読めない理由)。sha の印がなければ ValueError（原本が変わった）。"""
    readable = staging_path(data_dir, sha)
    if readable.exists():
        return readable.read_text(encoding="utf-8"), None
    unreadable = _unreadable_path(data_dir, sha)
    if unreadable.exists():
        return "", unreadable.read_text(encoding="utf-8")
    raise ValueError(_NOT_CONVERTED)


_ShaCache = dict[Path, "str | OSError"]


def _report(errors: list[dict[str, str]], source: str, error: OSError) -> None:
    """errors に入れる（同じ source は 1 回だけ）。"""
    if all(e["source"] != source for e in errors):
        errors.append({"source": source, "message": str(error)})


def _sha(cache: _ShaCache, path: Path) -> str:
    """原本の sha256（1 回だけ計算する。失敗も覚える）。開けなければ OSError。"""
    if path not in cache:
        try:
            cache[path] = convert.sha256_of(path)
        except OSError as error:
            cache[path] = error
    result = cache[path]
    if isinstance(result, OSError):
        raise result
    return result


def _body(path: Path) -> str:
    """ノートの本文（frontmatter の閉じ線より後）。"""
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    _, _, body = text.partition("\n---\n")
    return body


def _rewrite(note: bundle.Note, **changes: Any) -> bundle.Note:
    """ノートの frontmatter の一部を直す。本文はそのまま。"""
    meta = note.meta | changes
    bundle.write_note(note.path, meta, _body(note.path))
    return bundle.Note(note.path, meta, note.original)


def _find_match(
    vault: Vault,
    note: bundle.Note,
    candidates: list[Path],
    shas: _ShaCache,
    errors: list[dict[str, str]],
) -> tuple[Path | None, bool]:
    """sha256 がノートと同じ未登録の原本と、開けない原本があったか。"""
    uncertain = False
    for candidate in candidates:
        try:
            if _sha(shas, candidate) == note.meta.get("sha256"):
                return candidate, uncertain
        except OSError as error:
            _report(errors, vault.rel(candidate), error)
            uncertain = True
    return None, uncertain


def _follow(vault: Vault, note: bundle.Note, match: Path, target: Path, done: list[str]) -> None:
    """ノートを原本に対応する場所（target）へ移し（同じ場所なら移さず）、frontmatter を合わせる。"""
    old = vault.rel(note.path)
    size, mtime_ns = convert.stat_of(match)
    updates: dict[str, Any] = {
        "resource": match.relative_to(vault.base).as_posix(),
        "size": size,
        "mtime_ns": mtime_ns,
    }
    place = bundle.location(vault, match)
    if place:
        updates |= {"project": place[0], "category": place[1]}
    if target.exists():  # 呼び出し側で、別のファイルでないことを確かめてある
        _rewrite(note, **updates)
        done.append(f"**Followed** {old} の原本名を {match.name} に合わせた")
        return
    body = _body(note.path)
    move_file(note.path, target)
    bundle.write_note(target, note.meta | updates, body)
    done.append(f"**Followed** {old} → {vault.rel(target)}")


def _follow_orphans(
    vault: Vault,
    orphans: list[bundle.Note],
    unregistered: list[Path],
    shas: _ShaCache,
    done: list[str],
    errors: list[dict[str, str]],
) -> list[Path]:
    """原本のないノートを、sha256 が同じ未登録の原本の隣へ移す。なければ消す。"""
    remaining = list(unregistered)
    for note in orphans:
        match, uncertain = _find_match(vault, note, remaining, shas, errors)
        if match is None and uncertain:
            continue  # 開けない原本が本物かもしれない。消さずに次回に回す
        old = vault.rel(note.path)
        try:
            target = vault.note_path(match) if match else None
            if match and target and (not target.exists() or target.samefile(note.path)):
                remaining.remove(match)  # 失敗しても主張済み。次回の追従で再試行する
                _follow(vault, note, match, target, done)
            else:
                note.path.unlink()
                done.append(f"**Removed** {old}（原本が見つからない）")
        except OSError as error:
            _report(errors, old, error)
    return remaining


def _detect_changes(
    vault: Vault,
    live: list[bundle.Note],
    shas: _ShaCache,
    errors: list[dict[str, str]],
) -> list[Item]:
    """記録と stat が違う原本を sha256 で調べる。中身が変わったものを changed の項目で返す。"""
    changed = []
    for note in live:
        original = note.original
        try:
            size, mtime_ns = convert.stat_of(original)
            if (size, mtime_ns) == (note.meta.get("size"), note.meta.get("mtime_ns")):
                continue
            same = _sha(shas, original) == note.meta.get("sha256")
        except OSError as error:
            _report(errors, vault.rel(original), error)
            continue
        if same:
            try:
                _rewrite(note, size=size, mtime_ns=mtime_ns)
            except OSError as error:
                _report(errors, vault.rel(note.path), error)
            continue
        previous = {key: note.meta.get(key) for key in _PREVIOUS_KEYS}
        changed.append(_item(vault, original, "changed", previous))
    return changed


def _item(vault: Vault, path: Path, kind: str, previous: dict[str, Any] | None = None) -> Item:
    place = bundle.location(vault, path)
    if kind == "inbox":
        parent = path.parent
        names = {folder: name for name, folder in vault.project_folders()}
        project = None if parent == vault.base / INBOX_NAME else names.get(parent, parent.name)
        category = None
    else:
        project, category = place if place else (None, None)
    return {
        "source": vault.rel(path),
        "source_path": str(path),
        "kind": kind,
        "name": path.name,
        "project": project,
        "category": category,
        "readable": False,
        "reason": None,
        "text": None,
        "excerpt": "",
        "previous": previous,
    }


def _convert_item(vault: Vault, data_dir: Path, item: Item, shas: _ShaCache) -> None:
    """原本を変換し、staging に本文か読めない印を書く。開けなければ OSError。"""
    path = vault.path(item["source"])
    sha = _sha(shas, path)
    text, reason = convert.extract(path)
    if reason is None:
        staging = staging_path(data_dir, sha)
        atomic_write(staging, text)
        item["text"] = str(staging)
        item["excerpt"] = text[:EXCERPT_CHARS]
    else:
        atomic_write(_unreadable_path(data_dir, sha), reason)
    item["readable"] = reason is None
    item["reason"] = reason


def _context(vault: Vault) -> dict[str, Any]:
    all_notes = bundle.notes(vault)
    # 完了した案件（80_archive）は、新しい資料の行き先にしない
    counts: dict[str, dict[str, int]] = {
        project: dict.fromkeys(bundle.categories(vault, project), 0)
        for project, folder in vault.project_folders()
        if not folder.is_relative_to(vault.base / ARCHIVE_NAME)
    }
    names: dict[str, int] = {}
    for note in all_notes:
        place = bundle.location(vault, note.original)
        if place and place[0] in counts:
            counts[place[0]][place[1]] = counts[place[0]].get(place[1], 0) + 1
    for categories in counts.values():
        for category, count in categories.items():
            name = split_category(category)[1]
            names[name] = names.get(name, 0) + count
    return {"projects": counts, "category_names": names, "clients": bundle.clients(all_notes)}


def _collect(
    vault: Vault,
    data_dir: Path,
    done: list[str],
    errors: list[dict[str, str]],
) -> tuple[list[Item], int]:
    """直せるものを直し（done に記録）、判断が要る項目を変換して返す。

    変換するのは MAX_ITEMS 件まで（開けなかった項目は数えない）。残りの件数も返す。
    shortcut: apply で毎回 error になる項目が MAX_ITEMS 件以上あると、後ろの資料は返らない。
    """
    shas: _ShaCache = {}
    all_notes = bundle.notes(vault)
    orphans = [n for n in all_notes if not n.original.is_file()]
    live = [n for n in all_notes if n.original.is_file()]
    registered = {n.original for n in live}
    # 照合先は拡張子を問わない（読めない資料のノートも追従させる）。未登録の項目は対応する拡張子だけ
    others = [p for p in bundle.candidates(vault) if p not in registered]
    others = _follow_orphans(vault, orphans, others, shas, done, errors)
    unregistered = [p for p in others if is_supported(p.name)]
    items = _detect_changes(vault, live, shas, errors)
    items += [_item(vault, p, "inbox") for p in bundle.inbox_files(vault)]
    items += [_item(vault, p, "inbox") for _, p in bundle.project_root_files(vault)]
    items += [_item(vault, p, "unregistered") for p in unregistered]

    converted: list[Item] = []
    for number, item in enumerate(items):
        if len(converted) == MAX_ITEMS:
            return converted, len(items) - number
        try:
            _convert_item(vault, data_dir, item, shas)
        except OSError as error:
            _report(errors, item["source"], error)
        else:
            converted.append(item)
    return converted, 0


def _warnings(vault: Vault) -> list[str]:
    """案件フォルダが予約名（shared・knowledge）と衝突しているときの警告。"""
    found = []
    for root in PROJECT_ROOTS:
        folder = vault.base / root
        if not folder.is_dir():
            continue
        for child in sorted(folder.iterdir()):
            target = next(
                (t for name, t in _RESERVED_PROJECTS.items() if fold_name(child.name) == name),
                None,
            )
            if target and child.is_dir() and not is_link(child):
                found.append(
                    f"{root}/{child.name} は予約名の案件で、{target} と衝突します。"
                    f"中身を {target}/<区分> へ移して、このフォルダを消してください"
                )
    return found


def _prepare(vault: Vault, data_dir: Path, now: datetime) -> dict[str, Any]:
    done: list[str] = []
    errors: list[dict[str, str]] = []
    try:
        items, more = _collect(vault, data_dir, done, errors)
    finally:
        if done:
            bundle.append_log(vault, done, now)
    if not items:
        bundle.write_indexes(vault)
        release(data_dir)
    return {
        "vault": str(vault.base),
        "items": items,
        "more": more,
        "context": _context(vault),
        "done": done,
        "errors": errors,
        "warnings": _warnings(vault),
        "plan_path": (vault.base / PLAN_NAME).as_posix(),  # 手順書は / 区切りのまま使わせる
    }


def prepare(vault: Vault, data_dir: Path, now: datetime) -> dict[str, Any]:
    """ロックを取り、追従・削除・記録の更新をして、判断が要る項目を変換して返す。

    項目があるとロックを残す（apply が解く）。失敗したときは解く。
    """
    acquire(data_dir, now)
    try:
        return _prepare(vault, data_dir, now)
    except BaseException:
        release(data_dir)
        raise


def _check_plan_item(item: Any) -> None:
    """計画の 1 項目の形を確かめる。不正なら ValueError。"""
    if not isinstance(item, dict):
        raise ValueError(f"計画の項目が辞書ではありません: {item!r}")
    source = item.get("source")
    parts = PurePosixPath(source).parts if isinstance(source, str) else ()
    top = (INBOX_NAME, *PROJECT_ROOTS, SHARED_NAME, KNOWLEDGE_NAME)
    if len(parts) < 2 or parts[0] not in top or ".." in parts:
        raise ValueError(f"source は {'・'.join(top)} の下のパスにしてください: {source!r}")
    if item.get("kind") not in ("inbox", "unregistered", "changed"):
        raise ValueError(f"kind が不正です（{source}）: {item.get('kind')!r}")
    for key in ("title", "description", "doc_type", "client"):
        if not isinstance(item.get(key), str):
            raise ValueError(f"{key} は文字列にしてください（{source}）")
    tags = item.get("tags")
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise ValueError(f"tags は文字列の配列にしてください（{source}）")
    value = item.get("date")
    if value is not None:
        try:
            valid = isinstance(value, str) and date.fromisoformat(value).isoformat() == value
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(f"date は YYYY-MM-DD にしてください（{source}）: {value!r}")
    if item["kind"] == "inbox":
        for key in ("project", "category"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise ValueError(f"inbox の {key} は空でない文字列にしてください（{source}）")


def _check_folder(name: str) -> None:
    """フォルダ名として使えるか（Windows が末尾から削る空白・ドットも不可）。"""
    check_folder_name(name)
    if name[-1] in " .":
        raise ValueError(f"フォルダ名にできません（末尾が空白かドット）: {name!r}")


def _destination(vault: Vault, item: Item) -> Path:
    """inbox の項目の移動先フォルダ。案件・区分は既存に寄せ、なければ新設する名前にする。"""
    project = item["project"].strip()
    _check_folder(project)
    if project.startswith("."):
        raise ValueError(f"案件名は . で始められません: {project!r}")
    project = next(
        (p for p in bundle.projects(vault) if fold_name(p) == fold_name(project)), project
    )
    # 新しい案件には 99_その他 も作る（apply）ので、最初からあるものとして寄せる
    project_dir = vault.project_dir(project) or (
        vault.base / _RESERVED_PROJECTS.get(project, f"{PROJECTS_NAME}/{project}")
    )
    is_new = not project_dir.is_dir()
    existing = [DEFAULT_CATEGORY] if is_new else bundle.categories(vault, project)
    raw = item["category"].strip()
    category = resolve_category(existing, raw)
    if category is None:
        category = next_category_name(existing, raw)
        number, label = split_category(category)
        if number is not None and number > MAX_CATEGORY_NUMBER and category != DEFAULT_CATEGORY:
            raise ValueError(f"区分の番号は {MAX_CATEGORY_NUMBER} までです: {category!r}")
        _check_folder(label)
    _check_folder(category)
    folder = project_dir / category
    for path in (folder.parent, folder):
        if is_link(path):
            raise ValueError(f"移動先がリンクです: {vault.rel(path)}")
    if not folder.resolve().is_relative_to(vault.base.resolve()):
        raise ValueError(f"移動先が vault の外です: {vault.rel(folder)}")
    return folder


def _guard_note(vault: Vault, note_path: Path) -> None:
    """そこにある .md がこのツールのノートでなければ ValueError（上書きしない）。"""
    if note_path.exists() and not bundle.is_note(bundle.read_frontmatter(note_path)):
        raise ValueError(f"{vault.rel(note_path)} に別のファイルがあります")


def _write_item_note(
    vault: Vault,
    item: Item,
    original: Path,
    sha: str,
    converted: tuple[str, str | None],
    clients: list[str],
    now: datetime,
) -> Path:
    """原本のノートを notes_dir に書く（changed は未知の鍵を残す）。取引先は clients に足す。

    converted は prepare の (本文, 読めない理由)。読めないときは、説明が空なら理由を書く。
    """
    note_path = vault.note_path(original)
    place = bundle.location(vault, original)
    if place is None:
        raise ValueError(f"案件・区分の下にありません: {vault.rel(original)}")
    meta = bundle.read_frontmatter(note_path) if item["kind"] == "changed" else {}
    body, reason = converted
    size, mtime_ns = convert.stat_of(original)
    client = bundle.canonical_client(clients, item["client"])
    meta |= {
        "type": "document",
        "title": item["title"],
        "description": item["description"] or reason or "",
        "resource": original.relative_to(vault.base).as_posix(),
        "tags": item["tags"],
        "timestamp": now.isoformat(),
        "project": place[0],
        "category": place[1],
        "client": client,
        "doc_type": item["doc_type"],
        "date": item["date"],
        "sha256": sha,
        "size": size,
        "mtime_ns": mtime_ns,
        "readable": reason is None,
    }
    bundle.write_note(note_path, meta, body)
    if client and client not in clients:
        clients.append(client)
    return note_path


def _source_path(vault: Vault, item: Item) -> Path:
    """項目の原本のパス。外・リンク・資料でない名前・種別に合わない場所は ValueError。"""
    rel = item["source"]
    if "\\" in rel:
        raise ValueError(f"source に \\ は使えません: {rel!r}")
    source = vault.path(rel)
    if not source.resolve().is_relative_to(vault.base.resolve()):
        raise ValueError(f"vault の外です: {rel!r}")
    hidden = any(part.startswith(".") for part in PurePosixPath(rel).parts)  # .obsidian/ など
    if source.is_symlink() or is_ignored(source.name) or hidden:
        raise ValueError(f"資料として扱えないファイルです: {rel}")
    if not source.is_file():
        raise ValueError(f"{rel} がありません")
    if not bundle.is_original_file(source):
        raise ValueError(f"資料として扱えないファイルです: {rel}")
    note = vault.note_path(source)
    if item["kind"] == "inbox":
        roots = [vault.base / INBOX_NAME, *(folder for _, folder in vault.project_folders())]
        if source.parent not in roots:
            raise ValueError(
                f"inbox は {INBOX_NAME}/ か案件フォルダの直下のファイルだけです: {rel}"
            )
    elif bundle.location(vault, source) is None:
        raise ValueError(f"案件・区分の下にありません: {rel}")
    elif item["kind"] == "unregistered" and note.exists():
        raise ValueError(f"{vault.rel(note)} があり、登録済みです")
    elif item["kind"] == "changed" and (
        not note.exists() or not bundle.is_note(bundle.read_frontmatter(note))
    ):
        raise ValueError(f"{rel} のノートがありません")
    return source


def _apply_item(
    vault: Vault, data_dir: Path, item: Item, clients: list[str], now: datetime
) -> tuple[Item, str | None]:
    """1 項目を実行する。(結果, ログの 1 行。保留は None)。OSError・ValueError は 1 件の失敗。"""
    source = _source_path(vault, item)
    sha = convert.sha256_of(source)
    original = source
    if item["kind"] == "inbox":
        folder = _destination(vault, item)
        original = folder / source.name
        if original.exists():
            if source.samefile(original):
                raise ValueError(f"移動先が原本そのものです: {item['source']}")
            if convert.sha256_of(original) == sha:
                source.unlink()
                where = "/".join(bundle.location(vault, original) or (folder.name,))
                line = (
                    f"**Discarded** {source.name}（{where} に同じ中身）"
                    f" — 元: {source} / 既存: {original}"
                )
                return {
                    "source": item["source"],
                    "status": "discarded",
                    "note": None,
                    "message": None,
                    "original": str(original),
                }, line
            conflict.add(vault, source, original)  # 同名・別内容は、動かさず利用者の選択を待つ
            return {
                "source": item["source"],
                "status": "conflict",
                "note": None,
                "message": None,
                "original": str(original),
            }, None
        if len(vault.rel(original)) > MAX_PATH_LEN:
            raise ValueError(f"パスが {MAX_PATH_LEN} 文字を超えます: {vault.rel(original)}")
        longest = max(len(str(original)), len(str(vault.note_path(original))))
        if longest > MAX_ABS_PATH:  # ノートは .notes/ と .md の分だけ原本より長い
            raise ValueError(f"ノートを含む絶対パスが {MAX_ABS_PATH} 文字を超えます: {original}")
    if item["kind"] != "changed":
        _guard_note(vault, vault.note_path(original))
    converted = _converted(data_dir, sha)  # 動かす前に確かめる
    if original != source:
        folder = original.parent
        created = [path for path in (folder.parent, folder) if not path.exists()]
        try:
            move_file(source, original)
        except OSError:
            for path in reversed(created):  # 作りかけの案件・区分を残さない
                path.rmdir()
            raise
        if folder.parent in created:
            (folder.parent / DEFAULT_CATEGORY).mkdir(exist_ok=True)
    note = _write_item_note(vault, item, original, sha, converted, clients, now)
    if item["kind"] == "changed":
        status, line = "refreshed", f"**Refreshed** {vault.rel(note)}"
    else:
        status = "filed"
        project, category = bundle.location(vault, original)  # _write_item_note で確かめてある
        client = bundle.canonical_client(clients, item["client"])
        fields = "、".join(f for f in (item["doc_type"], client, item["date"] or "日付なし") if f)
        line = (
            f"**Filed** {original.name} → {project}/{category}（{fields}）"
            f" — 元: {source} / 先: {original}"
        )
    return {
        "source": item["source"],
        "status": status,
        "note": vault.rel(note),
        "message": None,
        "original": str(original),
    }, line


def apply(vault: Vault, data_dir: Path, plan: dict[str, Any], now: datetime) -> dict[str, Any]:
    """計画どおりに資料を置いてノートを書き、索引・ログを直してロックと staging を片づける。

    計画の形が不正なら ValueError（何も実行しない。ロックは残す）。1 件の失敗は error にして続ける。
    """
    items = plan.get("items") if isinstance(plan, dict) else None
    if not isinstance(items, list):
        raise ValueError("計画に items（配列）がありません")
    for item in items:
        _check_plan_item(item)
    results: list[Item] = []
    done: list[str] = []
    try:
        clients = bundle.clients(bundle.notes(vault))
        for item in items:
            try:
                result, line = _apply_item(vault, data_dir, item, clients, now)
            except (OSError, ValueError) as error:
                failed = {"source": item["source"], "status": "error", "note": None}
                results.append(failed | {"message": str(error), "original": None})
            else:
                results.append(result)
                if line:
                    done.append(line)
    except BaseException:
        _finish(vault, data_dir, done, now)  # 本体の例外を隠さない
        raise
    failure = _finish(vault, data_dir, done, now)
    if failure:
        raise failure
    return {"results": results, "done": done}


def _finish(vault: Vault, data_dir: Path, done: list[str], now: datetime) -> OSError | None:
    """ログ・索引・ロック・staging・計画ファイルの後始末。各段は独立で、最初の失敗を返す。"""
    steps = [
        lambda: bundle.append_log(vault, done, now) if done else None,
        lambda: bundle.write_indexes(vault),
        lambda: release(data_dir),
        lambda: shutil.rmtree(data_dir / "staging", ignore_errors=True),
        lambda: (vault.base / PLAN_NAME).unlink(missing_ok=True),
    ]
    failure = None
    for step in steps:
        try:
            step()
        except OSError as error:
            failure = failure or error
    return failure
