"""テストが束（vault の根）にファイルを置くための補助。"""

from pathlib import Path

from vault_kb.bundle import write_note
from vault_kb.vault import PROJECTS_NAME, SHARED_NAME, SHARED_PROJECT, Vault


def original_rel(project: str, category: str, name: str) -> str:
    """案件・区分・名前の原本の、vault の根からの相対パス（shared は 30_shared）。"""
    if project == SHARED_PROJECT:
        return f"{SHARED_NAME}/{category}/{name}"
    return f"{PROJECTS_NAME}/{project}/{category}/{name}"


def put(vault: Vault, rel: str, content: bytes = b"bytes") -> Path:
    """vault の根からの相対パスにファイルを置く（親フォルダも作る）。"""
    path = vault.path(rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def make_note(
    vault: Vault, project: str, category: str, name: str, body: str = "本文", **meta
) -> Path:
    """原本 <project>/<category>/<name> のノートを notes_dir に置き、そのパスを返す。

    原本は 20_projects/<project>/ の下（project が shared なら 30_shared/）にあるものとする。
    """
    rel = original_rel(project, category, name)
    full = {
        "type": "document",
        "title": name,
        "description": "説明",
        "resource": rel,
        "tags": ["t"],
        "timestamp": "2026-10-31T00:00:00+09:00",
        "project": project,
        "category": category,
        "client": None,
        "doc_type": None,
        "date": "2026-10-15",
        "sha256": "0" * 64,
        "size": 1,
        "mtime_ns": 1,
        "readable": True,
    } | meta
    vault.path(rel).parent.mkdir(parents=True, exist_ok=True)  # 原本のフォルダ
    path = vault.notes_dir / f"{rel}.md"
    write_note(path, full, body)
    return path
