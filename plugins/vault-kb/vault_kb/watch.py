"""状態の確認と、フォルダの変化の見張り。"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from . import bundle, conflict, convert
from .reconcile import _is_fresh, _lock_path
from .vault import INBOX_NAME, Vault, atomic_write, is_link, walk

STOP_NAME = "watch.stop"  # 置き場に書く、見張りへの停止の印


def _is_changed(note: bundle.Note) -> bool:
    """原本の (サイズ, 更新時刻) がノートの記録と違うか。"""
    try:
        return convert.stat_of(note.original) != (note.meta.get("size"), note.meta.get("mtime_ns"))
    except OSError:
        return False


def status(vault: Vault, data_dir: Path) -> dict[str, Any]:
    """片づけ待ちの件数と、片づけの状態。"""
    all_notes = bundle.notes(vault)
    registered = {note.original for note in all_notes}
    unregistered = [path for path in bundle.originals(vault) if path not in registered]
    existing = [note for note in all_notes if note.original.is_file()]
    counts = {
        "inbox": len(bundle.inbox_files(vault)),
        "project_root": len(bundle.project_root_files(vault)),
        "unregistered": len(unregistered),
        "orphan": len(all_notes) - len(existing),
        "changed": sum(_is_changed(note) for note in existing),
    }
    return {
        "vault": str(vault.root),
        **counts,
        "pending": sum(counts.values()),
        "conflicts": len(conflict.holds(vault)),
        "busy": _is_fresh(_lock_path(data_dir), datetime.now().astimezone()),
    }


def open_items(vault: Vault, data_dir: Path) -> dict[str, Any]:
    """見張りを止めるときの確認事項。未解消は片づけ待ちの件数と進行中のロック。読めない資料は参考の一覧。"""
    result = status(vault, data_dir)
    unreadable = [
        str(note.original)
        for note in bundle.notes(vault)
        if note.meta.get("readable") is False and note.original.is_file()
    ]
    unresolved = (
        result["pending"] + result["conflicts"] + int(result["busy"])
    )  # 読めない資料は利用者が直せない
    return {**result, "unreadable": unreadable, "unresolved": unresolved}


def request_stop(data_dir: Path, now: datetime) -> None:
    """見張りへの停止の印を書く（動いている見張りは、次の巡回で止まる）。"""
    atomic_write(data_dir / STOP_NAME, now.isoformat())


def _stop_requested_after(data_dir: Path, started: datetime) -> bool:
    """停止の印が、started 以降の時刻で書かれているか。読めない・形式が不正な印は無効。"""
    try:
        written = datetime.fromisoformat((data_dir / STOP_NAME).read_text(encoding="utf-8"))
        return written >= started
    except (OSError, ValueError, TypeError):  # 印がない・読めない・時刻が不正・タイムゾーンなし
        return False


def notify_lines(status: dict[str, Any]) -> list[str]:
    """セッション開始時に利用者へ伝える文。伝えることがなければ空。"""
    lines = []
    if status["pending"] > 0:
        lines.append(
            f"片づけ待ちが {status['pending']} 件あります"
            "（/vault-kb:watch か /vault-kb:ask で片づきます）"
        )
    if status["conflicts"] > 0:
        lines.append(
            f"同名の確認待ちが {status['conflicts']} 件あります（/vault-kb:resolve で選べます）"
        )
    return lines


def snapshot(vault: Vault) -> dict[str, tuple[int, int]]:
    """00_inbox と案件フォルダの下の資料（予約名を除く）の {相対パス: (サイズ, 更新時刻 ns)}。"""
    roots = [vault.base / INBOX_NAME, *(folder for _, folder in vault.project_folders())]
    found = {}
    for root in roots:
        if not root.is_dir() or is_link(root):
            continue
        _, files = walk(root)
        for path in files:
            hidden = any(part.startswith(".") for part in path.relative_to(vault.base).parts)
            if hidden or not bundle.is_original_file(path):  # .git・.obsidian などは資料ではない
                continue
            try:
                found[vault.rel(path)] = convert.stat_of(path)
            except OSError:
                continue  # 一覧のあとで消えたファイルは無かったことにする
    return found


def watch_events(
    vault: Vault,
    data_dir: Path,
    poll_seconds: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    rounds: int | None = None,
) -> Iterator[str]:
    """基準と違い、次の poll でも同じ（落ち着いた）なら "changed" を出す。rounds は上限回数。

    別の片づけが進行中（ロックが生きている）の間は出さない。基準を進めずに待ち、終わったあとで出す。

    開始より後に書かれた停止の印があれば "stopped" を出して終わる（前の印は無視する）。
    """
    started = datetime.now().astimezone()
    baseline = previous = snapshot(vault)
    count = 0
    while rounds is None or count < rounds:
        count += 1
        sleep(poll_seconds)
        if _stop_requested_after(data_dir, started):
            yield "stopped"
            return
        current = snapshot(vault)
        busy = _is_fresh(_lock_path(data_dir), datetime.now().astimezone())
        if current != baseline and current == previous and not busy:
            yield "changed"
            baseline = current
        previous = current
