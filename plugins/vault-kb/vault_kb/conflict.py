"""同名・別内容の資料の保留（vault 直下の .vault-kb-conflicts.json）と、その解決。"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from . import convert
from .vault import INBOX_NAME, Vault, atomic_write, check_folder_name, is_ignored, is_link

CONFLICTS_NAME = ".vault-kb-conflicts.json"  # . 始まりなので走査が無視する
_DEVICE_NAME = re.compile(
    r"(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?", re.IGNORECASE
)  # Windows の予約名
_FIELDS = {"source": str, "destination": str, "size": int, "mtime_ns": int}

Record = dict[str, Any]


def _path(vault: Vault) -> Path:
    return vault.base / CONFLICTS_NAME


def _is_inside(rel: str) -> bool:
    """vault からの相対パスとして使えるか（絶対パス・.. ・\\・NUL 文字は不可）。"""
    parts = PurePosixPath(rel).parts
    return (
        bool(parts)
        and "\\" not in rel
        and "\0" not in rel
        and not PurePosixPath(rel).is_absolute()
        and ".." not in parts
    )


def _well_formed(record: Any) -> bool:
    return (
        isinstance(record, dict)
        and all(type(record.get(key)) is kind for key, kind in _FIELDS.items())
        and _is_inside(record["source"])
        and _is_inside(record["destination"])
    )


def _load(vault: Vault) -> list[Record]:
    """ファイルの記録（形が正しいものだけ）。読めない・壊れていれば空。"""
    try:
        data = json.loads(_path(vault).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [r for r in data if _well_formed(r)] if isinstance(data, list) else []


def _usable_name(name: str) -> bool:
    """Windows のファイル名として使えるか（使えない文字・末尾のドットか空白・予約名は不可）。"""
    try:
        check_folder_name(name)
    except ValueError:
        return False
    return (
        not name.endswith((" ", ".")) and not is_ignored(name) and not _DEVICE_NAME.fullmatch(name)
    )


def _has_link(vault: Vault, rel: str) -> bool:
    """vault から rel までの、どの階層かがリンクか（20_projects・80_archive 自体を含む）。"""
    path = vault.base
    for part in PurePosixPath(rel).parts:
        path = path / part
        if is_link(path):
            return True
    return False


def _places(vault: Vault, record: Record) -> tuple[Path, Path] | None:
    """記録が apply の作る形なら (source, destination)。違えば None。

    記録のファイルは信頼できない（同期・手編集で変わりうる）ので、resolve が消す・置き換える
    パスを、00_inbox か案件フォルダ直下の資料と、区分の下の原本だけに限る。
    """
    source, destination = vault.path(record["source"]), vault.path(record["destination"])
    projects = {folder for _, folder in vault.project_folders()}
    parts = (*PurePosixPath(record["source"]).parts, *PurePosixPath(record["destination"]).parts)
    ok = (
        source != destination
        and source.parent in {vault.base / INBOX_NAME, *projects}
        and destination.parent.parent in projects
        and not any(part.startswith(".") for part in parts)
        and _usable_name(source.name)
        and _usable_name(destination.name)
        and not _has_link(vault, record["source"])
        and not _has_link(vault, record["destination"])
    )
    return (source, destination) if ok else None


def holds(vault: Vault) -> list[Record]:
    """有効な保留（形が正しく、資料があり、記録の size・mtime_ns と同じもの）。"""
    valid = []
    for record in _load(vault):
        try:
            places = _places(vault, record)
            stamp = convert.stat_of(places[0]) if places else None
        except (OSError, ValueError):  # 資料がない・OS がパスとして受け付けない（長さ・文字）
            continue
        if stamp is None:
            continue
        if stamp == (record["size"], record["mtime_ns"]):
            valid.append(record)
    return valid


def held_sources(vault: Vault) -> set[Path]:
    return {vault.path(record["source"]) for record in holds(vault)}


def add(vault: Vault, source: Path, destination: Path) -> None:
    """source を保留にする（同じ source の記録と、無効な記録は捨てる）。"""
    size, mtime_ns = convert.stat_of(source)
    record = {
        "source": vault.rel(source),
        "destination": vault.rel(destination),
        "size": size,
        "mtime_ns": mtime_ns,
    }
    others = [r for r in holds(vault) if r["source"] != record["source"]]
    _save(vault, [*others, record])


def _save(vault: Vault, records: list[Record]) -> None:
    atomic_write(_path(vault), json.dumps(records, ensure_ascii=False, indent=2))


def _mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")


def listing(vault: Vault) -> list[dict[str, Any]]:
    """保留の一覧（元と先のフルパス、サイズ、更新日時）。先がなければ先の項目は null。"""
    found = []
    for record in holds(vault):
        source, destination = vault.path(record["source"]), vault.path(record["destination"])
        there = destination.is_file()
        found.append(
            {
                "source": record["source"],
                "source_path": str(source),
                "source_size": source.stat().st_size,
                "source_mtime": _mtime(source),
                "destination_path": str(destination),
                "destination_size": destination.stat().st_size if there else None,
                "destination_mtime": _mtime(destination) if there else None,
            }
        )
    return found


def resolve(vault: Vault, source_rel: str, action: str) -> str:
    """保留を解決する（overwrite＝先を置き換える、discard＝後続を削除する）。log.md の行を返す。"""
    record = next((r for r in holds(vault) if r["source"] == source_rel), None)
    if record is None:
        raise ValueError(f"{source_rel} は同名の確認待ちではありません")
    source, destination = _places(vault, record)  # holds が確かめた形
    if action == "overwrite":
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, destination)
        line = f"**Overwrote** {destination.name} — 元: {source} / 先: {destination}"
    else:
        source.unlink()
        line = f"**Discarded** {source.name}（後続を削除） — 元: {source} / 既存: {destination}"
    _save(vault, [r for r in _load(vault) if r["source"] != source_rel])
    return line
