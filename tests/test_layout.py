"""vault の中のフォルダ構成（00_inbox・20_projects・30_shared・80_archive・利用者の領域）。"""

import os
import shutil
import subprocess
from datetime import datetime, timezone

import pytest
from helpers import put
from vault_kb import bundle, watch
from vault_kb.reconcile import apply, prepare, release
from vault_kb.vault import init_vault, install_obsidian_assets

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
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
        "50_prompts",
        "80_archive",
        "90_attachments",
    ]
    assert (vault.root / "30_shared" / "99_その他").is_dir()
    assert (vault.root / "40_knowledge" / "99_その他").is_dir()


def test_project_foldersは4つの置き場の案件を順に返し他のフォルダは案件にしない(vault):
    (vault.base / "20_projects" / "B社" / "01_見積").mkdir(parents=True)
    (vault.base / "20_projects" / ".hidden").mkdir()
    (vault.base / "80_archive" / "A社").mkdir()
    (vault.base / "Z社").mkdir()
    (vault.base / "10_daily" / "日報").mkdir()

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
        "50_prompts/指示.md",
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
    release(data_dir)  # 項目があるとロックが残る（apply が解く）
    result = _file(vault, data_dir, "A社", "見積")

    assert [r["status"] for r in result["results"]] == ["filed"]
    assert (vault.base / "20_projects" / "A社" / "01_見積" / "見積.txt").is_file()
    assert list((vault.base / "00_inbox").iterdir()) == []
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
    release(data_dir)
    _file(vault, data_dir, "shared", "99_その他")

    assert (vault.base / "30_shared" / "99_その他" / "規程.txt").is_file()
    meta = bundle.read_frontmatter(vault.notes_dir / "30_shared/99_その他/規程.txt.md")
    assert (meta["resource"], meta["project"]) == ("30_shared/99_その他/規程.txt", "shared")


def test_80_archiveにある案件を指定すると80_archiveの中へ片づけ索引に印が付く(vault, data_dir):
    (vault.base / "80_archive" / "旧案件" / "01_見積").mkdir(parents=True)
    put(vault, "00_inbox/見積.txt", TEXT)

    _file(vault, data_dir, "旧案件", "見積")

    assert (vault.base / "80_archive" / "旧案件" / "01_見積" / "見積.txt").is_file()
    assert not (vault.base / "20_projects" / "旧案件").exists()
    index = (vault.base / "index.md").read_text(encoding="utf-8")
    assert "- [旧案件（アーカイブ）](<.notes/80_archive/旧案件/index.md>) — 1 件\n" in index


def test_案件を80_archiveへ動かすとノートが追従し_新しい資料の行き先の候補から外れる(
    vault, data_dir
):
    put(vault, "00_inbox/見積.txt", TEXT)
    _file(vault, data_dir, "A社", "見積")
    assert "A社" in prepare(vault, data_dir, NOW)["context"]["projects"]

    (vault.base / "20_projects" / "A社").rename(vault.base / "80_archive" / "A社")
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


def _junction(link, target):
    """link を target へのジャンクション（リンクの一種）にする。作れない環境では飛ばす。"""
    command = ["cmd", "/c", "mklink", "/J", str(link), str(target)]
    result = subprocess.run(command, capture_output=True, check=False)  # noqa: S607
    if result.returncode != 0:
        pytest.skip("ジャンクションを作れない環境")


def test_リンクの案件と30_shared_40_knowledgeは案件にならず走査もされない(
    vault, data_dir, tmp_path
):
    outside = tmp_path / "外" / "X社" / "01_x"
    outside.mkdir(parents=True)
    (outside / "a.txt").write_bytes(TEXT)
    _junction(vault.base / "20_projects" / "X社", outside.parent)
    shutil.rmtree(vault.base / "30_shared")
    _junction(vault.base / "30_shared", outside.parent)
    shutil.rmtree(vault.base / "40_knowledge")
    _junction(vault.base / "40_knowledge", outside.parent)

    assert bundle.projects(vault) == []
    assert watch.snapshot(vault) == {}
    assert watch.status(vault, data_dir)["pending"] == 0


@pytest.mark.parametrize("name", ["30_shared", "40_knowledge"])
def test_init_vaultはリンクの30_shared_40_knowledgeの先に99_その他を作らない(vault, tmp_path, name):
    outside = tmp_path / "外"
    outside.mkdir()
    shutil.rmtree(vault.base / name)
    _junction(vault.base / name, outside)

    init_vault(vault.root)

    assert list(outside.iterdir()) == []


def test_00_inboxがリンクのとき中のファイルは片づけの対象にならない(vault, data_dir, tmp_path):
    outside = tmp_path / "外"
    outside.mkdir()
    (outside / "g.txt").write_bytes(TEXT)
    (vault.base / "00_inbox").rmdir()
    _junction(vault.base / "00_inbox", outside)

    assert bundle.inbox_files(vault) == []
    assert watch.snapshot(vault) == {}
    assert watch.status(vault, data_dir)["pending"] == 0


def test_テンプレートの置き場がリンクのときはリンク先へ書かず他のフォルダには置く(tmp_path):
    root = tmp_path / "v"
    root.mkdir()
    outside = tmp_path / "外"
    outside.mkdir()
    _junction(root / "91_templates", outside)

    install_obsidian_assets(root)

    assert list(outside.iterdir()) == []
    assert (root / "92_bases" / "その日のノート.base").is_file()


def _broken_junction(link, tmp_path):
    """先を消して、壊れたジャンクションにする（exists() も is_symlink() も False になる）。"""
    target = tmp_path / "消えた先"
    target.mkdir()
    _junction(link, target)
    target.rmdir()


@pytest.mark.parametrize("name", ["00_inbox", "30_shared", "40_knowledge"])
def test_init_vaultは壊れたリンクのフォルダがあっても落ちず何も作らない(vault, tmp_path, name):
    shutil.rmtree(vault.base / name)
    _broken_junction(vault.base / name, tmp_path)

    init_vault(vault.root)

    assert not (vault.base / name).exists()
    assert (vault.base / "20_projects").is_dir()


def test_テンプレートの置き場が壊れたリンクでも落ちず他のフォルダには置く(tmp_path):
    root = tmp_path / "v"
    root.mkdir()
    _broken_junction(root / "91_templates", tmp_path)

    install_obsidian_assets(root)

    assert (root / "92_bases" / "その日のノート.base").is_file()


@pytest.mark.parametrize(
    "broken", ["20_projects/X社", "20_projects/X社/01_見積"], ids=["案件", "区分"]
)
def test_移動先が壊れたリンクなら_リンクだと伝えて原本もリンクも動かさない(
    vault, data_dir, tmp_path, broken
):
    put(vault, "00_inbox/見積.txt", TEXT)
    (vault.base / broken).parent.mkdir(exist_ok=True)  # 区分が壊れているときは、案件は実在する
    _broken_junction(vault.base / broken, tmp_path)

    result = _file(vault, data_dir, "X社", "見積")["results"][0]

    assert result["status"] == "error"
    assert result["message"] == f"移動先がリンクです: {broken}"
    assert (vault.base / "00_inbox" / "見積.txt").is_file()
    assert os.path.lexists(vault.base / broken)


def test_40_knowledge直下の資料は区分へ移りノートは_notes_40_knowledgeにできる(vault, data_dir):
    put(vault, "40_knowledge/手順メモ.txt", TEXT)

    items = prepare(vault, data_dir, NOW)["items"]
    assert [(i["source"], i["kind"], i["project"]) for i in items] == [
        ("40_knowledge/手順メモ.txt", "inbox", "knowledge")
    ]
    release(data_dir)
    result = _file(vault, data_dir, "knowledge", "手順")

    assert [r["status"] for r in result["results"]] == ["filed"]
    assert (vault.base / "40_knowledge" / "01_手順" / "手順メモ.txt").is_file()
    assert not (vault.base / "40_knowledge" / "手順メモ.txt").exists()
    meta = bundle.read_frontmatter(vault.notes_dir / "40_knowledge/01_手順/手順メモ.txt.md")
    assert (meta["resource"], meta["project"], meta["category"]) == (
        "40_knowledge/01_手順/手順メモ.txt",
        "knowledge",
        "01_手順",
    )
    assert "knowledge" in (vault.base / "index.md").read_text(encoding="utf-8")
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
    assert (vault.base / "40_knowledge" / "99_その他" / "ノウハウ.txt").is_file()
    assert not (vault.base / "20_projects" / "KNOWLEDGE").exists()
    assert not (vault.base / "20_projects" / "knowledge").exists()
