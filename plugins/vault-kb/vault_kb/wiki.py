"""wiki の統合ページ: 資料の指紋と、前回からの差分の分類。"""

from __future__ import annotations

import hashlib
from typing import Any


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
