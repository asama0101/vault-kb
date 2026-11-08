"""片づけの排他。置き場の reconcile.lock に取得時刻を書き、15 分で失効させる。"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

LOCK_STALE_SECONDS = 900


class Busy(Exception):
    """別の片づけが進行中（ロックが 15 分以内）。"""


def lock_path(data_dir: Path) -> Path:
    return data_dir / "reconcile.lock"


def _create_lock(path: Path, now: datetime) -> None:
    """ロックを新しく作る（あれば FileExistsError）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(handle, "w", encoding="utf-8") as file:
        file.write(token_of(now))


def is_fresh(path: Path, now: datetime) -> bool:
    try:
        written = datetime.fromisoformat(path.read_text(encoding="utf-8").strip())
        # 未来の時刻（時計が戻った）を生きているとみなすと、いつまでも取れない
        return 0 <= (now - written).total_seconds() < LOCK_STALE_SECONDS
    except (OSError, ValueError, TypeError):
        return False


def acquire(data_dir: Path, now: datetime) -> None:
    """ロックを取る。進行中（15 分未満）なら Busy。古い・読めないロックは消して取り直す。"""
    path = lock_path(data_dir)
    # shortcut: 古いロックの引き継ぎは、2 本が同時に「古い」と見ると両方が取れる。
    # 落ちたロックが残った直後に、ask と見張りが同時に動くときだけ。同名の保留があるので壊れはしない
    busy = Busy("別の片づけが進行中です。15 分後にもう一度試してください。")
    try:
        _create_lock(path, now)
    except FileExistsError:
        if is_fresh(path, now):
            raise busy from None
        path.unlink(missing_ok=True)
        try:
            _create_lock(path, now)
        except FileExistsError:
            raise busy from None


def token_of(now: datetime) -> str:
    """ロックの持ち主の印。ロックに書く取得時刻そのもの（取り直されると別の値になる）。"""
    return now.isoformat()


def holder(data_dir: Path) -> str | None:
    """いまのロックの持ち主の印。ロックがない・読めなければ None。"""
    try:
        return lock_path(data_dir).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def owns(data_dir: Path, token: str | None) -> bool:
    """このロックを片づけてよいか。印がない（token なし）か、ロックがないか、印が同じなら True。

    失効したロックを別の実行が取り直したあとは False（その実行の作業を壊さない）。
    """
    return token is None or holder(data_dir) in (None, token)


def release(data_dir: Path, token: str | None = None) -> None:
    """ロックを解く。token を渡したときは、持ち主の印が同じときだけ解く。"""
    if owns(data_dir, token):
        lock_path(data_dir).unlink(missing_ok=True)
