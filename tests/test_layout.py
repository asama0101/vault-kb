"""vault の中のフォルダ構成（00_inbox・20_projects・30_shared・80_archive・利用者の領域）。"""

from datetime import UTC, datetime

import pytest
from helpers import put
from vault_kb import bundle, watch
from vault_kb.reconcile import apply, prepare

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
TEXT = "これは二十字以上ある日本語の資料の本文です。確認用の文章。".encode()


def _file(vault, data_dir, project, category):
    """prepare が返す 1 件を、指定の案件・区分で片づける（apply の結果を返す）。"""
    items = prepare(vault, data_dir, NOW)["items"]
    assert len(items) == 1
    item = {
        "source": items[0]["source"],
        "kind": items[0]["kind"],
        "project": project,
        "category": category,
        "title": "題",
        "description": "説明",
        "tags": ["t"],
        "client": "",
        "doc_type": "",
        "date": None,
    }
    return apply(vault, data_dir, {"items": [item]}, NOW)


def test_init_vaultはvaultの中に8つのフォルダを作りvault_kbは作らない(vault):
    names = sorted(p.name for p in vault.root.iterdir())

    assert names == [
        "00_inbox",
        "10_daily",
        "20_projects",
        "30_shared",
        "40_knowledge",
        "80_archive",
        "90_attachments",
    ]
    assert (vault.root / "30_shared" / "99_その他").is_dir()
    assert (vault.root / "40_knowledge" / "99_その他").is_dir()


def test_project_foldersは4つの置き場の案件を順に返し他のフォルダは案件にしない(vault):
    (vault.root / "20_projects" / "B社" / "01_見積").mkdir(parents=True)
    (vault.root / "20_projects" / ".hidden").mkdir()
    (vault.root / "80_archive" / "A社").mkdir()
    (vault.root / "Z社").mkdir()
    (vault.root / "10_daily" / "日報").mkdir()

    assert [name for name, _ in vault.project_folders()] == ["B社", "A社", "shared", "knowledge"]
    assert bundle.projects(vault) == ["A社", "B社", "knowledge", "shared"]


@pytest.mark.parametrize(
    ("rel", "expected"),
    [
        ("20_projects/A/01_見積/x.xlsx", ("A", "01_見積")),
        ("80_archive/A/01_見積/sub/x.xlsx", ("A", "01_見積")),
        ("30_shared/99_その他/x.pdf", ("shared", "99_その他")),
        ("20_projects/A/x.xlsx", None),
        ("20_projects/A/01_見積", None),
        ("30_shared/x.pdf", None),
        ("40_knowledge/99_その他/x.pdf", ("knowledge", "99_その他")),
        ("40_knowledge/手順/sub/x.pdf", ("knowledge", "手順")),
        ("40_knowledge/x.pdf", None),
        ("00_inbox/x.pdf", None),
        ("10_daily/2026-10-31.md", None),
        ("A/01_見積/x.pdf", None),
        ("x.pdf", None),
    ],
)
def test_locationは区分より下のファイルだけ案件と区分を返す(vault, rel, expected):
    assert bundle.location(vault, vault.path(rel)) == expected


def test_利用者の領域とvault直下のファイルは片づけの対象にならない(vault, data_dir):
    for rel in (
        "10_daily/2026-10-31.md",
        "90_attachments/図.pdf",
        "91_templates/議事録.md",
        "直下.txt",
        "Z社/01_見積/x.txt",
    ):
        put(vault, rel, TEXT)

    assert watch.status(vault, data_dir)["pending"] == 0
    assert watch.snapshot(vault) == {}
    assert prepare(vault, data_dir, NOW)["items"] == []


def test_00_inboxの資料は20_projectsの案件と区分へ移りノートはnotesの同じパスにできる(
    vault, data_dir
):
    put(vault, "00_inbox/見積.txt", TEXT)

    items = prepare(vault, data_dir, NOW)["items"]
    assert [(i["source"], i["kind"], i["project"]) for i in items] == [
        ("00_inbox/見積.txt", "inbox", None)
    ]
    result = _file(vault, data_dir, "A社", "見積")

    assert [r["status"] for r in result["results"]] == ["filed"]
    assert (vault.root / "20_projects" / "A社" / "01_見積" / "見積.txt").is_file()
    assert list((vault.root / "00_inbox").iterdir()) == []
    meta = bundle.read_frontmatter(vault.notes_dir / "20_projects/A社/01_見積/見積.txt.md")
    assert (meta["resource"], meta["project"], meta["category"]) == (
        "20_projects/A社/01_見積/見積.txt",
        "A社",
        "01_見積",
    )


def test_30_shared直下のファイルは案件sharedのinboxになる(vault, data_dir):
    put(vault, "30_shared/規程.txt", TEXT)

    items = prepare(vault, data_dir, NOW)["items"]
    assert [(i["source"], i["kind"], i["project"]) for i in items] == [
        ("30_shared/規程.txt", "inbox", "shared")
    ]
    _file(vault, data_dir, "shared", "99_その他")

    assert (vault.root / "30_shared" / "99_その他" / "規程.txt").is_file()
    meta = bundle.read_frontmatter(vault.notes_dir / "30_shared/99_その他/規程.txt.md")
    assert (meta["resource"], meta["project"]) == ("30_shared/99_その他/規程.txt", "shared")


def test_80_archiveにある案件を指定すると80_archiveの中へ片づけ索引に印が付く(vault, data_dir):
    (vault.root / "80_archive" / "旧案件" / "01_見積").mkdir(parents=True)
    put(vault, "00_inbox/見積.txt", TEXT)

    _file(vault, data_dir, "旧案件", "見積")

    assert (vault.root / "80_archive" / "旧案件" / "01_見積" / "見積.txt").is_file()
    assert not (vault.root / "20_projects" / "旧案件").exists()
    index = (vault.root / "index.md").read_text(encoding="utf-8")
    assert "- [旧案件（アーカイブ）](<.notes/80_archive/旧案件/index.md>) — 1 件\n" in index


def test_案件を80_archiveへ動かすとノートが追従し_新しい資料の行き先の候補から外れる(
    vault, data_dir
):
    put(vault, "00_inbox/見積.txt", TEXT)
    _file(vault, data_dir, "A社", "見積")
    assert "A社" in prepare(vault, data_dir, NOW)["context"]["projects"]

    (vault.root / "20_projects" / "A社").rename(vault.root / "80_archive" / "A社")
    result = prepare(vault, data_dir, NOW)

    assert result["items"] == []
    assert [line[: len("**Followed**")] for line in result["done"]] == ["**Followed**"]
    assert "A社" not in result["context"]["projects"]
    assert "shared" in result["context"]["projects"]
    new_note = vault.notes_dir / "80_archive/A社/01_見積/見積.txt.md"
    meta = bundle.read_frontmatter(new_note)
    assert (meta["resource"], meta["project"], meta["category"]) == (
        "80_archive/A社/01_見積/見積.txt",
        "A社",
        "01_見積",
    )
    assert not (vault.notes_dir / "20_projects" / "A社").exists()


def test_40_knowledge直下の資料は区分へ移りノートは_notes_40_knowledgeにできる(vault, data_dir):
    put(vault, "40_knowledge/手順メモ.txt", TEXT)

    items = prepare(vault, data_dir, NOW)["items"]
    assert [(i["source"], i["kind"], i["project"]) for i in items] == [
        ("40_knowledge/手順メモ.txt", "inbox", "knowledge")
    ]
    result = _file(vault, data_dir, "knowledge", "手順")

    assert [r["status"] for r in result["results"]] == ["filed"]
    assert (vault.root / "40_knowledge" / "01_手順" / "手順メモ.txt").is_file()
    assert not (vault.root / "40_knowledge" / "手順メモ.txt").exists()
    meta = bundle.read_frontmatter(vault.notes_dir / "40_knowledge/01_手順/手順メモ.txt.md")
    assert (meta["resource"], meta["project"], meta["category"]) == (
        "40_knowledge/01_手順/手順メモ.txt",
        "knowledge",
        "01_手順",
    )
    assert "knowledge" in (vault.root / "index.md").read_text(encoding="utf-8")
    assert (vault.notes_dir / "40_knowledge" / "01_手順" / "index.md").is_file()


def test_40_knowledgeの区分直下の資料は未登録として登録される(vault, data_dir):
    put(vault, "40_knowledge/調査/メモ.txt", TEXT)

    assert watch.status(vault, data_dir)["unregistered"] == 1
    assert list(watch.snapshot(vault)) == ["40_knowledge/調査/メモ.txt"]
    items = prepare(vault, data_dir, NOW)["items"]
    assert [(i["source"], i["kind"], i["project"], i["category"]) for i in items] == [
        ("40_knowledge/調査/メモ.txt", "unregistered", "knowledge", "調査")
    ]


def test_00_inboxの資料を案件knowledgeに指定すると40_knowledgeに置かれ20_projectsには作られない(
    vault, data_dir
):
    put(vault, "00_inbox/ノウハウ.txt", TEXT)

    result = _file(vault, data_dir, "KNOWLEDGE", "99_その他")

    assert [r["status"] for r in result["results"]] == ["filed"]
    assert (vault.root / "40_knowledge" / "99_その他" / "ノウハウ.txt").is_file()
    assert not (vault.root / "20_projects" / "KNOWLEDGE").exists()
    assert not (vault.root / "20_projects" / "knowledge").exists()
