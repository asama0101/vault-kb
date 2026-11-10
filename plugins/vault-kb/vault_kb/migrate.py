"""旧形式（2.2.0 まで）のノートと .wiki/ のページを、新しい形式へ一括で直す。

直すのは frontmatter だけで、本文は 1 文字も変えない。resource の先頭 `/`、
timestamp → generated、evidence → sources、統合ページ・回答の title・description。
既定は dry-run で、書くのは apply=True のときだけ。書き換える前のファイルは、置き場の
backup/ に残す。title・description の文言は wiki.py の _plan_page・_plan_query と合わせる。
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from . import bundle, wiki
from .lock import Busy, acquire, release
from .vault import RESERVED_NAMES, Vault, atomic_write

_KEY_LINE = re.compile(r"([^:\s][^:]*):\s*(.*)")
LEGACY_BY = "vault-kb/legacy"  # 旧形式のノートを作った版は分からない
Change = tuple[Path, str, str]  # (パス, 元の全文, 新しい全文)


def _supported(raw: str) -> bool:
    """frontmatter が、このプラグインが書く形（1 行 1 項目、値は JSON、キーの重複なし）か。"""
    lines = raw.split("\n")
    if lines[0] != "---" or "---" not in lines[1:]:
        return False
    keys = []
    for line in lines[1 : lines.index("---", 1)]:
        match = _KEY_LINE.fullmatch(line)
        if not match:
            return False
        if match[2]:
            try:
                json.loads(match[2])
            except json.JSONDecodeError:
                return False
        keys.append(match[1])
    return len(keys) == len(set(keys))


def _is_aware(value: Any) -> bool:
    """UTC オフセット付きの ISO 8601 の文字列か（OKF の generated.at の条件）。"""
    try:
        return isinstance(value, str) and datetime.fromisoformat(value).utcoffset() is not None
    except ValueError:
        return False


def _wiki_texts(meta: dict[str, Any], path: Path) -> tuple[str, str] | None:
    """統合ページ・回答の (title, description)（機械的に作る）。対象外なら None。"""
    sources = meta.get("sources") if isinstance(meta.get("sources"), list) else []
    if meta.get("type") == "query":
        question = meta["question"] if isinstance(meta.get("question"), str) else path.stem
        return question, f"質問「{question}」への回答（出典 {len(sources)} 件）"
    unread = meta.get("unread") if isinstance(meta.get("unread"), list) else []
    count = len(sources) + len(unread)
    if isinstance(meta.get("theme"), str):
        return meta["theme"], f"テーマ「{meta['theme']}」の統合ページ（ノート {count} 件）"
    name = meta["project"] if isinstance(meta.get("project"), str) else path.stem
    return name, f"{name}の統合ページ（ノート {count} 件）"


def _converted(meta: dict[str, Any], path: Path, in_wiki: bool) -> dict[str, Any]:
    meta = dict(meta)
    if "timestamp" in meta:
        if "generated" in meta:
            del meta["timestamp"]
        elif _is_aware(meta["timestamp"]):  # オフセットのない値は generated にせず、残す
            meta["generated"] = {"by": LEGACY_BY, "at": meta.pop("timestamp")}
    if in_wiki:
        if "evidence" in meta and "sources" not in meta:
            meta["sources"] = meta.pop("evidence")
        title, description = _wiki_texts(meta, path)  # type は呼び出し側で確かめてある
        meta.setdefault("title", title)
        meta.setdefault("description", description)
    return meta


def _rebuild(meta: dict[str, Any], raw: str) -> str:
    """新しい frontmatter に、元の本文（閉じ線より後ろ）をバイト列のまま付ける。"""
    lines = raw.split("\n")
    body_start = sum(len(line) + 1 for line in lines[: lines.index("---", 1) + 1])
    return bundle.frontmatter_text(meta) + raw[body_start:]


def _candidates(vault: Vault) -> list[tuple[Path, bool]]:
    """見る対象の (パス, .wiki/ の中か)。索引・予約名・隠しフォルダ（.bak）・リンクは除く。"""
    found = []
    for root, in_wiki in ((vault.notes_dir, False), (vault.wiki_dir, True)):
        if not root.is_dir() or root.is_symlink():
            continue
        for path in sorted(root.rglob("*.md")):
            parts = path.relative_to(root).parts
            hidden = any(part.startswith(".") for part in parts)
            if path.name in RESERVED_NAMES or hidden or path.is_symlink():
                continue
            found.append((path, in_wiki))
    return found


def _plan(vault: Vault) -> tuple[list[Change], list[dict[str, str]]]:
    """直す対象（変更案）と、壊さないよう触らないファイル（理由つき）。何も書かない。"""
    changes: list[Change] = []
    skipped: list[dict[str, str]] = []
    for path, in_wiki in _candidates(vault):
        try:
            raw = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            skipped.append({"path": vault.rel(path), "reason": "UTF-8 で読めない"})
            continue
        if not raw.startswith("---"):
            continue  # frontmatter のない利用者のメモ
        meta = bundle.read_frontmatter(path)
        target = meta.get("type") in ("synthesis", "query") if in_wiki else bundle.is_note(meta)
        if not target:
            continue
        if not _supported(raw):
            skipped.append({"path": vault.rel(path), "reason": "frontmatter が 1 行 1 項目でない"})
            continue
        new = _rebuild(_converted(meta, path, in_wiki), raw)
        if new != raw:
            changes.append((path, raw, new))
    return changes, skipped


def _write(vault: Vault, backup: Path, change: Change) -> str | None:
    """1 件を直す。書く直前に読み直し、変わっていたら書かない（理由を返す）。"""
    path, raw, new = change
    if path.read_bytes().decode("utf-8") != raw:
        return "確認のあとに内容が変わった"
    target = backup / vault.rel(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, target)
    atomic_write(path, new)
    return None


def run(vault: Vault, data_dir: Path, now: datetime, apply: bool) -> dict[str, Any]:
    """旧形式のファイルを数える（apply なら直す）。

    戻り値: changed（直した／直す対象の vault 相対パス）、skipped（触らなかったファイルと
    理由）、errors（書き込みに失敗したファイル。失敗しても続ける）、backup（バックアップ先。
    書かなければ None）。ロックが取れなければ busy。
    """
    if apply:
        try:
            acquire(data_dir, now)
        except Busy:
            return {"busy": True}
    try:
        changes, skipped = _plan(vault)
        result: dict[str, Any] = {
            "dry_run": not apply,
            "changed": [vault.rel(path) for path, _, _ in changes],
            "skipped": skipped,
            "errors": [],
            "backup": None,
        }
        if not apply:
            return result
        backup = data_dir / "backup" / f"migrate-{now:%Y%m%d-%H%M%S}"
        done: list[Path] = []
        for change in changes:
            try:
                reason = _write(vault, backup, change)
            except (OSError, UnicodeDecodeError) as error:
                result["errors"].append({"path": vault.rel(change[0]), "message": str(error)})
                continue
            if reason:
                skipped.append({"path": vault.rel(change[0]), "reason": reason})
            else:
                done.append(change[0])
        result["changed"] = [vault.rel(path) for path in done]
        if done:
            result["backup"] = str(backup)
        if any(vault.wiki_dir in path.parents for path in done):
            wiki.write_wiki_index(vault)
        return result
    finally:
        if apply:
            release(data_dir)
