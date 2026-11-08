"""wiki の統合ページ: 資料の指紋と、前回からの差分の分類。"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from vault_kb import bundle
from vault_kb.lock import Busy, acquire, release
from vault_kb.vault import Vault, atomic_write, check_folder_name

READ_LIMIT = 30  # 1 回に読む資料の数
FULL_READ_LIMIT = 100  # full のときの上限
SLUG_LIMIT = 40  # 保存した回答のファイル名の題の字数
_STRIKE = re.compile(r"~~.+?~~")
_SLUG_DROP = re.compile(r'[\\/:*?"<>|.\s\x00-\x1f]')  # 区切り文字・使えない文字・空白・ドット


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


def _strings(request: dict[str, Any], key: str, default: Any = None) -> list[str]:
    value = request.get(key, default)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{key} は文字列の一覧で指定してください")
    return value


def _md_name(request: dict[str, Any], key: str) -> str:
    """.wiki/ の下で読み書きするファイル名。区切り・..・ドライブ・使えない文字・.md 以外は不可。"""
    name = _text(request, key)
    try:
        check_folder_name(name)
        bad = not name.endswith(".md") or ".." in name
    except ValueError:
        bad = True
    if bad:
        raise ValueError(f"{key} が .wiki/queries/ の外を指しています: {name!r}")
    return name


def _inside(vault: Vault, path: Path) -> Path:
    """組み立てたパスが .wiki/ の中にあることを確かめる。"""
    if not path.resolve().is_relative_to(vault.wiki_dir.resolve()):
        raise ValueError(f".wiki/ の外には書けません: {path}")
    return path


def _check_query(vault: Vault, request: dict[str, Any]) -> dict[str, Any]:
    file = _md_name(request, "file")
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


def _replace_moved(text: str, moves: dict[str, str]) -> str:
    """移動した資料の旧 resource を新 resource に置き換える。

    長い順の 1 回の置換なので、連鎖（a→b、b→a）と接頭辞（見積.xlsx と 見積.xlsx.bak）を壊さない。
    """
    if not moves:
        return text
    olds = "|".join(re.escape(old) for old in sorted(moves, key=len, reverse=True))
    # 境界は ASCII だけで見る（日本語の文に接したパスも置き換える）
    pattern = rf"(?<![A-Za-z0-9_./-])(?:{olds})(?![A-Za-z0-9_/-]|\.[A-Za-z0-9])"
    return re.sub(pattern, lambda m: moves[m[0]], text)


Write = list[tuple[Path, str]]


def _plan_page(vault: Vault, op: dict[str, Any], kind: str, now: datetime) -> tuple[Write, str]:
    """統合ページ・テーマページの書き込み（退避を含む）と、log.md の行（移動だけなら空）。"""
    name = _text(op, "name")
    check_folder_name(name)
    expected = _text(op, "expected_digest")
    body = op.get("body")
    if body is not None and not isinstance(body, str):
        raise ValueError("body は文字列で指定してください")
    if kind == "project":
        if vault.project_dir(name) is None:
            raise ValueError(f"案件がありません: {name!r}")
        entries = project_entries(vault, name)
        if len(entries) < 2:
            raise ValueError(f"ノートが 2 件未満の案件には統合ページを作りません: {name!r}")
    else:
        selected = _strings(op, "selected")
        existing = {e["resource"]: e for e in _entries(vault)}
        unknown = [r for r in selected if r not in existing]
        if unknown or not selected:
            raise ValueError(f"selected に存在しない資料があります（または空）: {unknown}")
        entries = [existing[r] for r in dict.fromkeys(selected)]
    current = digest(entries)
    if expected != current:
        raise ValueError("確認の後に資料の指紋が変わりました。wiki-check からやり直してください")
    path = _inside(vault, vault.wiki_dir / f"{kind}s" / f"{name}.md")
    page = read_page(path)
    meta, old_body = page if page else ({}, "")
    old_sources = meta["sources"] if _valid_sources(meta.get("sources")) else []
    old_unread = meta.get("unread")
    old_unread = old_unread if isinstance(old_unread, list) else []
    # 前回の未読は sources にないので、比べる相手から除く（除かないと全部「追加」になる）
    diff = classify(old_sources, [e for e in entries if e["resource"] not in old_unread])
    moves = {m["from"]: m["to"] for m in diff["moved"]}
    if body is None:
        if page is None:
            raise ValueError("ページがないので body を省略できません")
        if diff["added"] or diff["changed"] or diff["removed"]:
            raise ValueError("移動のほかに差分があるので body が要ります")
        body, unread = old_body, old_unread
    else:
        unread = _strings(op, "unread", [])
    names = {e["resource"] for e in entries}
    if not set(unread) <= names:
        raise ValueError(f"unread に対象の資料でないものがあります: {sorted(set(unread) - names)}")
    body = _replace_moved(body, moves)
    if len(_STRIKE.findall(body)) < len(_STRIKE.findall(old_body)):
        raise ValueError("取り消し線が減っています。古い記述は消さず、取り消し線にしてください")
    new_meta = {
        "type": "synthesis",
        kind: name,
        "timestamp": now.isoformat(),
        "sources_digest": current,
        "sources": [
            {"resource": e["resource"], "sha256": e["sha256"]}
            for e in sorted_by_resource(entries)
            if e["resource"] not in unread
        ],
        "unread": unread,
    }
    writes: Write = []
    if page is not None:  # 1 世代だけ退避する
        backup = _inside(vault, vault.wiki_dir / ".bak" / kind / f"{name}.md")
        writes.append((backup, path.read_bytes().decode("utf-8", errors="replace")))
    writes.append((path, bundle.note_text(new_meta, body)))
    if "body" not in op:
        return writes, ""
    if kind == "project":
        return writes, f"**Synthesized** {name}（ノート {len(entries)} 件）"
    found = [bundle.location(vault, vault.path(e["resource"])) for e in entries]
    projects = len({loc[0] for loc in found if loc})
    return writes, f"**Synthesized** テーマ「{name}」（ノート {len(entries)} 件、{projects} 案件）"


def _plan_query(
    vault: Vault, op: dict[str, Any], now: datetime, planned: dict[Path, str]
) -> tuple[Path, str]:
    """保存する回答のパスと全文。sha256 と scope_digest は今のノートから作る。"""
    question = _text(op, "question")
    answer = _text(op, "answer")
    evidence = _strings(op, "evidence")
    scope = _strings(op, "scope")
    slug = _text(op, "slug")
    existing = {e["resource"]: e["sha256"] for e in _entries(vault)}
    missing = [r for r in evidence if r not in existing]
    if missing:
        raise ValueError(f"evidence に存在しない資料があります: {missing}")
    folder = vault.wiki_dir / "queries"
    if "overwrite" in op:
        file = _md_name(op, "overwrite")
        if not (folder / file).is_file():
            raise ValueError(f"overwrite のファイルがありません: {file!r}")
    else:
        stem = f"{now:%Y%m%d-%H%M%S}-{_SLUG_DROP.sub('', slug)[:SLUG_LIMIT] or 'answer'}"
        file, number = f"{stem}.md", 2
        while (folder / file).exists() or folder / file in planned:
            file, number = f"{stem}-{number}.md", number + 1
    meta = {
        "type": "query",
        "question": question,
        "timestamp": now.isoformat(),
        "evidence": [{"resource": r, "sha256": existing[r]} for r in dict.fromkeys(evidence)],
        "scope": scope,
        "scope_digest": scope_digest(vault, scope),
    }
    return _inside(vault, folder / file), bundle.note_text(meta, answer)


def apply(vault: Vault, data_dir: Path, request: dict[str, Any], now: datetime) -> dict[str, Any]:
    """.wiki/ に書く。片づけと同じロックの下で、全部の op を検証してから書く。"""
    try:
        acquire(data_dir, now)
    except Busy:
        return {"busy": True}
    try:
        ops = request.get("ops")
        if not isinstance(ops, list) or not all(isinstance(op, dict) for op in ops):
            raise ValueError("ops は op の一覧で指定してください")
        writes: dict[Path, str] = {}
        written: list[Path] = []
        logs: list[str] = []
        for op in ops:
            if op.get("op") == "page":
                kind = op.get("kind")
                if kind not in ("project", "theme"):
                    raise ValueError(f"kind は project か theme にしてください: {kind!r}")
                planned, line = _plan_page(vault, op, kind, now)
                logs += [line] if line else []
            elif op.get("op") == "query":
                planned = [_plan_query(vault, op, now, writes)]
            else:
                raise ValueError(f"op は page か query にしてください: {op.get('op')!r}")
            for path, text in planned:
                if path in writes:
                    raise ValueError(f"同じファイルに 2 回書こうとしています: {vault.rel(path)}")
                writes[path] = text
            written.append(planned[-1][0])
        # ここまでで検証は終わり。ここから書く
        if writes:
            vault.hide_wiki_dir()
        for path, text in writes.items():
            atomic_write(path, text)
        if logs:
            bundle.append_log(vault, logs, now)
        return {"written": [vault.rel(path) for path in written], "logged": len(logs)}
    finally:
        release(data_dir)
