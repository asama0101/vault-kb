"""wiki の統合ページ: 資料の指紋と、前回からの差分の分類。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from vault_kb import bundle
from vault_kb.vault import Vault, check_folder_name

READ_LIMIT = 30  # 1 回に読む資料の数
FULL_READ_LIMIT = 100  # full のときの上限


def digest(entries: list[dict[str, Any]]) -> str:
    """資料の一覧の指紋。`resource` で並べるので、順序に依存しない。"""
    text = "".join(f"{e['resource']}\t{e['sha256']}\n" for e in sorted_by_resource(entries))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sorted_by_resource(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(entries, key=lambda e: e["resource"])


def classify(sources: list[dict[str, Any]], current: list[dict[str, Any]]) -> dict[str, list[Any]]:
    """前回の資料（sources）と今の資料（current）の差を、追加・変更・削除・移動に分ける。"""
    old = {e["resource"]: e for e in sources}
    new = {e["resource"]: e for e in current}
    # (1) resource と sha256 が両方一致するものは変化なし
    old_left = [
        e
        for e in sorted_by_resource(sources)
        if e["resource"] not in new or new[e["resource"]]["sha256"] != e["sha256"]
    ]
    new_left = [
        e
        for e in sorted_by_resource(current)
        if e["resource"] not in old or old[e["resource"]]["sha256"] != e["sha256"]
    ]
    # (2) 相手の resource がないものだけを、sha256 が同じもの同士で 1 対 1 に組にする
    moved: list[dict[str, str]] = []
    gone = {e["resource"] for e in old_left}
    fresh = {e["resource"] for e in new_left}
    for s in old_left:
        if s["resource"] in new:
            continue
        for c in new_left:
            if c["resource"] in old or c["resource"] not in fresh or c["sha256"] != s["sha256"]:
                continue
            moved.append({"from": s["resource"], "to": c["resource"]})
            gone.discard(s["resource"])
            fresh.discard(c["resource"])
            break
    # (3) 残りを追加・変更・削除に分ける
    return {
        "added": [e for e in new_left if e["resource"] in fresh and e["resource"] not in old],
        "changed": [e for e in new_left if e["resource"] in old],
        "removed": [r for r in sorted(gone) if r not in new],
        "moved": moved,
    }


def read_page(path: Path) -> tuple[dict[str, Any], str] | None:
    """ページの (frontmatter, 本文)。ファイルがなければ None。"""
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    lines = text.split("\n")
    close = next((i for i, line in enumerate(lines[1:], 1) if line == "---"), 0)
    if lines[0] != "---" or not close:
        return {}, text.strip()
    return bundle.read_frontmatter(path), "\n".join(lines[close + 1 :]).strip()


def _entries(vault: Vault, folder: Path | None = None) -> list[dict[str, Any]]:
    """原本が実在するノートの {resource, sha256, date}。folder があればその下の原本だけ。"""
    found = []
    for note in bundle.notes(vault):
        if not note.original.is_file():
            continue
        if folder is not None and not note.original.is_relative_to(folder):
            continue
        date = note.meta.get("date")
        found.append(
            {
                "resource": note.meta["resource"],
                "sha256": note.meta["sha256"],
                "date": date if isinstance(date, str) else "",
            }
        )
    return sorted(sorted_by_resource(found), key=lambda e: e["date"], reverse=True)


def project_entries(vault: Vault, project: str) -> list[dict[str, Any]]:
    """案件のフォルダの下に原本が実在するノートの一覧（date の新しい順、日付なしは最後）。"""
    folder = vault.project_dir(project)
    return [] if folder is None else _entries(vault, folder)


def scope_digest(vault: Vault, scope: list[str]) -> str:
    """範囲の指紋。scope が空なら vault 全体、そうでなければ指定した案件の分。"""
    if not scope:
        return digest(_entries(vault))
    return digest([e for project in scope for e in project_entries(vault, project)])


def _valid_sources(value: Any) -> bool:
    return isinstance(value, list) and all(
        isinstance(e, dict)
        and isinstance(e.get("resource"), str)
        and isinstance(e.get("sha256"), str)
        for e in value
    )


def _text(request: dict[str, Any], key: str) -> str:
    value = request.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} は文字列で指定してください")
    return value


def _check_page(vault: Vault, request: dict[str, Any], kind: str) -> dict[str, Any]:
    """project・theme の状態。theme は selected の資料だけを対象にする。"""
    name = _text(request, "name")
    check_folder_name(name)
    full = request.get("full", False)
    if not isinstance(full, bool):
        raise ValueError("full は true か false で指定してください")
    page = read_page(vault.wiki_dir / f"{kind}s" / f"{name}.md")
    meta = page[0] if page else {}
    broken = page is not None and not _valid_sources(meta.get("sources"))
    sources = [] if page is None or broken else meta["sources"]
    extra: dict[str, Any] = {}
    if kind == "project":
        entries = project_entries(vault, name)
        too_few = len(entries) < 2
    else:
        selected = request.get("selected")
        if not isinstance(selected, list) or not all(isinstance(r, str) for r in selected):
            raise ValueError("selected は文字列の一覧で指定してください")
        existing = {e["resource"]: e for e in _entries(vault)}
        entries = [existing[r] for r in dict.fromkeys(selected) if r in existing]
        deselected = [s["resource"] for s in sources if s["resource"] in existing]
        deselected = [r for r in deselected if r not in selected]
        sources = [s for s in sources if s["resource"] not in deselected]
        extra = {
            "unknown": [r for r in dict.fromkeys(selected) if r not in existing],
            "deselected": deselected,
        }
        too_few = not entries
    diff = classify(sources, entries)
    unread = meta.get("unread")
    unread = unread if isinstance(unread, list) else []
    now = digest(entries)
    if too_few:
        action = "none" if page is not None and kind == "project" else "skip"
    elif page is None:
        action = "create"
    elif full or broken or meta.get("sources_digest") != now or unread:
        action = "update"
    else:
        action = "none"
    pending = entries if full else diff["added"] + diff["changed"]
    pending = sorted(sorted_by_resource(pending), key=lambda e: e["date"], reverse=True)
    limit = FULL_READ_LIMIT if full else READ_LIMIT
    return {
        "kind": kind,
        "name": name,
        "exists": page is not None,
        "broken": broken,
        "note_count": len(entries),
        "action": action,
        "digest": now,
        "diff": {
            "added": len(diff["added"]),
            "changed": len(diff["changed"]),
            "moved": diff["moved"],
            "removed": diff["removed"],
        },
        "read_list": pending[:limit],
        "deferred": [e["resource"] for e in pending[limit:]],
        "unread": unread,
    } | extra


def _check_query(vault: Vault, request: dict[str, Any]) -> dict[str, Any]:
    file = _text(request, "file")
    if not file.endswith(".md") or any(x in file for x in ("/", "\\")) or ".." in file:
        raise ValueError(f"file が .wiki/queries/ の外を指しています: {file!r}")
    page = read_page(vault.wiki_dir / "queries" / file)
    meta = page[0] if page else {}
    evidence = meta.get("evidence")
    valid = _valid_sources(evidence)
    now = {e["resource"]: e["sha256"] for e in _entries(vault)}
    used = evidence if valid else []
    changed = [
        e["resource"] for e in used if e["resource"] in now and now[e["resource"]] != e["sha256"]
    ]
    missing = [e["resource"] for e in used if e["resource"] not in now]
    scope = meta.get("scope")
    scope_ok = isinstance(scope, list) and all(isinstance(p, str) for p in scope)
    scope_changed = not scope_ok or meta.get("scope_digest") != scope_digest(vault, scope)
    empty = not used
    return {
        "kind": "query",
        "file": file,
        "exists": page is not None,
        "empty": empty,
        "reusable": page is not None
        and not empty
        and not changed
        and not missing
        and not scope_changed,
        "changed": changed,
        "missing": missing,
        "scope_changed": page is not None and scope_changed,
    }


def check(vault: Vault, request: dict[str, Any]) -> dict[str, Any]:
    """統合ページ・テーマページ・保存した回答の状態を調べる（結果は小さく保つ）。"""
    kind = request.get("kind")
    if kind in ("project", "theme"):
        return _check_page(vault, request, kind)
    if kind == "query":
        return _check_query(vault, request)
    raise ValueError(f"kind は project・theme・query のどれかにしてください: {kind!r}")
