import msvcrt
import os
import subprocess
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from helpers import make_note, original_rel, put
from markitdown import MarkItDown
from okf_check import check_bundle
from sample_docs import make_pdf, make_txt
from vault_kb import convert, reconcile
from vault_kb.bundle import read_frontmatter, write_indexes
from vault_kb.lock import Busy, acquire, release
from vault_kb.reconcile import (
    MAX_ITEMS,
    PLAN_NAME,
    apply,
    prepare,
    staging_path,
)

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
TEXT = "これは二十字以上ある日本語の資料の本文です。確認用の文章。"
MISSING = "変換結果がありません（原本が変わった可能性）。次回に回します"


@contextmanager
def locked(path):
    """path の先頭 1 バイトをロックする間、ほかから読めなくする（使用中のファイル）。"""
    with path.open("r+b") as file:
        msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            yield
        finally:
            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)


def junction(link, target):
    """link に target へのジャンクションを作る（作れなければスキップ）。"""
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],  # noqa: S607 - Windows の cmd
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("ジャンクションを作れない（環境要因）")


def register(vault, project, category, name, text=TEXT):
    """原本と、実際の指紋を持つノートを置き、原本のパスを返す。"""
    original = vault.path(original_rel(project, category, name))
    original.parent.mkdir(parents=True, exist_ok=True)
    make_txt(original, text)
    size, mtime_ns = convert.stat_of(original)
    make_note(
        vault,
        project,
        category,
        name,
        sha256=convert.sha256_of(original),
        size=size,
        mtime_ns=mtime_ns,
    )
    return original


def test_acquireは15分未満のロックでBusy_15分過ぎれば取り直せる(data_dir):
    acquire(data_dir, NOW)
    with pytest.raises(Busy):
        acquire(data_dir, NOW + timedelta(minutes=14))
    later = NOW + timedelta(minutes=16)
    acquire(data_dir, later)
    assert (data_dir / "reconcile.lock").read_text(encoding="utf-8") == later.isoformat()
    release(data_dir)
    assert not (data_dir / "reconcile.lock").exists()


def test_壊れたロックは古いものとして取り直せる(data_dir):
    (data_dir / "reconcile.lock").write_text("壊れた", encoding="utf-8")
    acquire(data_dir, NOW)
    assert (data_dir / "reconcile.lock").read_text(encoding="utf-8") == NOW.isoformat()


def test_未来の時刻のロックは時計が戻ったものとして取り直せる(data_dir):
    (data_dir / "reconcile.lock").write_text((NOW + timedelta(hours=1)).isoformat(), "utf-8")
    acquire(data_dir, NOW)
    assert (data_dir / "reconcile.lock").read_text(encoding="utf-8") == NOW.isoformat()


def test_prepareはinboxのファイルをkind_inboxで返し本文をstagingに書く(vault, data_dir):
    make_txt(vault.root / "00_inbox" / "x.txt", TEXT)
    result = prepare(vault, data_dir, NOW)
    (item,) = result["items"]
    assert item["kind"] == "inbox"
    assert item["source"] == "00_inbox/x.txt"
    assert item["name"] == "x.txt"
    assert item["source_path"] == str(vault.root / "00_inbox" / "x.txt")
    assert result["vault"] == str(vault.root)
    assert Path(item["source_path"]).is_absolute()
    assert item["project"] is None
    assert item["category"] is None
    assert item["readable"] is True
    assert item["reason"] is None
    assert item["previous"] is None
    assert item["text"] == str(
        staging_path(data_dir, convert.sha256_of(vault.root / "00_inbox" / "x.txt"))
    )
    assert TEXT in Path(item["text"]).read_text(encoding="utf-8")


def test_prepareは本文の先頭4000字をexcerptで返し_読めない資料は空(vault, data_dir):
    make_txt(vault.root / "00_inbox" / "long.txt", "あ" * 3999 + "い" + "う" * 600)
    (vault.root / "00_inbox" / "scan.zip").write_bytes(b"PK\x03\x04dummy")

    items = {item["name"]: item for item in prepare(vault, data_dir, NOW)["items"]}

    assert items["long.txt"]["excerpt"] == "あ" * 3999 + "い"
    assert items["scan.zip"]["excerpt"] == ""


def test_prepareは計画ファイルのパスをvault直下の隠しファイルで返し_それを資料にしない(
    vault, data_dir
):
    first = prepare(vault, data_dir, NOW)
    Path(first["plan_path"]).write_text('{"items": []}', encoding="utf-8")

    second = prepare(vault, data_dir, NOW)

    assert first["plan_path"] == (vault.root / ".vault-kb-plan.json").as_posix()
    assert (second["items"], second["done"]) == ([], [])


def test_prepareは案件直下のファイルをその案件のinboxとして返す(vault, data_dir):
    (vault.root / "20_projects" / "A").mkdir()
    make_txt(vault.root / "20_projects" / "A" / "x.txt", TEXT)
    (item,) = prepare(vault, data_dir, NOW)["items"]
    assert (item["kind"], item["project"], item["category"]) == ("inbox", "A", None)
    assert item["source"] == "20_projects/A/x.txt"


def test_prepareは区分フォルダのノートのない原本をunregisteredで返す(vault, data_dir):
    folder = vault.root / "20_projects" / "A" / "01_x"
    folder.mkdir(parents=True)
    make_txt(folder / "f.txt", TEXT)
    (item,) = prepare(vault, data_dir, NOW)["items"]
    assert (item["kind"], item["project"], item["category"]) == ("unregistered", "A", "01_x")
    assert item["source"] == "20_projects/A/01_x/f.txt"
    assert item["previous"] is None


def test_prepareは原本だけ動かされたノートをshaで追従し_resourceと案件区分を直す(vault, data_dir):
    original = register(vault, "A", "01_x", "f.txt")
    target = vault.root / "20_projects" / "B" / "02_y"
    target.mkdir(parents=True)
    original.rename(target / "f.txt")
    result = prepare(vault, data_dir, NOW)
    assert result["items"] == []
    assert result["done"] == [
        "**Followed** .notes/20_projects/A/01_x/f.txt.md → .notes/20_projects/B/02_y/f.txt.md"
    ]
    assert not (vault.notes_dir / "20_projects" / "A" / "01_x" / "f.txt.md").exists()
    new_note = vault.notes_dir / "20_projects" / "B" / "02_y" / "f.txt.md"
    meta = read_frontmatter(new_note)
    assert (meta["resource"], meta["project"], meta["category"]) == (
        "20_projects/B/02_y/f.txt",
        "B",
        "02_y",
    )
    assert meta["title"] == "f.txt"
    assert "本文" in new_note.read_text(encoding="utf-8")
    assert "**Followed**" in (vault.root / "log.md").read_text(encoding="utf-8")


def test_prepareは追従先に同名のmdがあれば孤児を消してRemovedを書く(vault, data_dir):
    original = register(vault, "A", "01_x", "f.txt")
    target = vault.root / "20_projects" / "B" / "02_y"
    target.mkdir(parents=True)
    original.rename(target / "f.txt")
    memo = put(vault, ".notes/20_projects/B/02_y/f.txt.md", "ノートではないメモ".encode())
    result = prepare(vault, data_dir, NOW)
    assert result["done"] == [
        "**Removed** .notes/20_projects/A/01_x/f.txt.md（原本が見つからない）"
    ]
    assert not (vault.notes_dir / "20_projects" / "A" / "01_x" / "f.txt.md").exists()
    assert memo.read_text(encoding="utf-8") == "ノートではないメモ"
    assert [(i["kind"], i["name"]) for i in result["items"]] == [("unregistered", "f.txt")]


def test_prepareは案件フォルダごと改名されたらノートを追従させ_古い案件の索引を残さない(
    vault, data_dir
):
    register(vault, "A", "01_x", "f.txt")
    write_indexes(vault)
    (vault.root / "20_projects" / "A").rename(vault.root / "20_projects" / "B")
    result = prepare(vault, data_dir, NOW)
    assert result["items"] == []
    assert result["done"] == [
        "**Followed** .notes/20_projects/A/01_x/f.txt.md → .notes/20_projects/B/01_x/f.txt.md"
    ]
    meta = read_frontmatter(vault.notes_dir / "20_projects" / "B" / "01_x" / "f.txt.md")
    assert (meta["resource"], meta["project"], meta["category"]) == (
        "20_projects/B/01_x/f.txt",
        "B",
        "01_x",
    )
    assert not (vault.notes_dir / "20_projects" / "A").exists()
    assert (vault.notes_dir / "20_projects" / "B" / "01_x" / "index.md").exists()


def test_prepareは原本が消えたノートを消しRemovedを書く(vault, data_dir):
    original = register(vault, "A", "01_x", "f.txt")
    make_txt(original.parent / "g.txt", "全く別の内容の資料です。二十字を超える長さにする。")
    original.unlink()
    result = prepare(vault, data_dir, NOW)
    expected = "**Removed** .notes/20_projects/A/01_x/f.txt.md（原本が見つからない）"
    assert result["done"] == [expected]
    assert not vault.note_path(original).exists()
    assert expected in (vault.root / "log.md").read_text(encoding="utf-8")
    assert [i["name"] for i in result["items"]] == ["g.txt"]


def test_resourceのない利用者のmdは原本として項目にし書き換えない(vault, data_dir):
    text = f"---\ntitle: a\n---\n{TEXT}\n"
    memo = put(vault, "20_projects/A/01_x/memo.md", text.encode())
    result = prepare(vault, data_dir, NOW)
    assert memo.read_bytes() == text.encode()
    assert result["done"] == []
    (item,) = result["items"]
    assert (item["source"], item["kind"], item["readable"]) == (
        "20_projects/A/01_x/memo.md",
        "unregistered",
        True,
    )


def test_typeとresourceだけで指紋のない利用者のmdも原本として項目にする(vault, data_dir):
    text = f"---\ntype: document\nresource: 議事録.docx\n---\n{TEXT}\n"
    memo = put(vault, "20_projects/A社/01_見積/会議メモ.md", text.encode())
    result = prepare(vault, data_dir, NOW)
    assert memo.read_bytes() == text.encode()
    assert [i["name"] for i in result["items"]] == ["会議メモ.md"]
    assert result["done"] == []


def test_ノートの形のmdは原本にしない(vault, data_dir):
    original = register(vault, "A社", "01_見積", "x.txt")
    note = original.parent / "x.txt.md"
    note.write_bytes(vault.note_path(original).read_bytes())  # 区分フォルダに置かれたノートの形
    before = note.read_bytes()
    result = prepare(vault, data_dir, NOW)
    assert note.read_bytes() == before
    assert (result["done"], result["items"]) == ([], [])


def test_予約名のmdは原本にしない(vault, data_dir):
    put(vault, "20_projects/A/01_x/index.md", TEXT.encode())
    put(vault, "log.md", TEXT.encode())
    result = prepare(vault, data_dir, NOW)
    assert result["items"] == []


def test_prepareは中身の変わった原本をchangedで返す(vault, data_dir):
    original = register(vault, "A", "01_x", "f.txt")
    make_txt(original, "書き換えた後の本文です。二十字を超える長さにしてあります。")
    (item,) = prepare(vault, data_dir, NOW)["items"]
    assert (item["kind"], item["project"], item["category"]) == ("changed", "A", "01_x")
    assert item["previous"] == {
        "title": "f.txt",
        "description": "説明",
        "tags": ["t"],
        "client": None,
        "doc_type": None,
        "date": "2026-10-15",
    }


def test_prepareはmtimeだけの変化なら記録を直すだけ(vault, data_dir):
    original = register(vault, "A", "01_x", "f.txt")
    size, mtime_ns = convert.stat_of(original)
    new_mtime = mtime_ns + 5 * 10**9
    os.utime(original, ns=(new_mtime, new_mtime))
    result = prepare(vault, data_dir, NOW)
    assert result["items"] == []
    assert result["done"] == []
    meta = read_frontmatter(vault.note_path(original))
    assert (meta["size"], meta["mtime_ns"]) == (size, new_mtime)
    assert not (vault.root / "log.md").exists()


def test_prepareは読めないファイルをreadable_falseと理由つきで返す(vault, data_dir):
    make_pdf(vault.root / "00_inbox" / "scan.pdf", "")
    (item,) = prepare(vault, data_dir, NOW)["items"]
    assert item["readable"] is False
    assert "文字を取り出せません" in item["reason"]
    assert item["text"] is None


def test_prepareは開けないファイルをerrorsに入れて他を続ける(vault, data_dir):
    make_txt(vault.root / "00_inbox" / "a.txt", TEXT)
    make_txt(vault.root / "00_inbox" / "b.txt", TEXT + "別")
    with locked(vault.root / "00_inbox" / "a.txt"):
        result = prepare(vault, data_dir, NOW)
    assert result["errors"] == [
        {"source": "00_inbox/a.txt", "message": "[Errno 13] Permission denied"}
    ]
    assert [i["name"] for i in result["items"]] == ["b.txt"]
    assert (vault.root / "00_inbox" / "a.txt").exists()


def test_prepareはinboxのmdを項目にしIgnoredを書かない(vault, data_dir):
    put(vault, "00_inbox/memo.md", TEXT.encode())
    result = prepare(vault, data_dir, NOW)
    assert [(i["name"], i["kind"]) for i in result["items"]] == [("memo.md", "inbox")]
    assert result["done"] == []


def test_applyはinboxのmdを原本のまま案件区分へ移し_md_mdのノートを書く(vault, data_dir):
    text = f"# 議事録\n\n{TEXT}\n".encode()
    put(vault, "00_inbox/memo.md", text)
    result = run_apply(vault, data_dir, {})
    assert result["results"][0]["status"] == "filed"
    assert result["results"][0]["note"] == ".notes/20_projects/A社/01_見積/memo.md.md"
    folder = vault.root / "20_projects" / "A社" / "01_見積"
    assert (folder / "memo.md").read_bytes() == text
    note = vault.notes_dir / "20_projects" / "A社" / "01_見積" / "memo.md.md"
    assert read_frontmatter(note)["resource"] == "20_projects/A社/01_見積/memo.md"
    assert not (vault.root / "00_inbox" / "memo.md").exists()
    assert check_bundle(vault.root) == []
    assert prepare(vault, data_dir, NOW + timedelta(minutes=1))["items"] == []


def test_mdの原本を編集するとchanged_消すとノートも消える(vault, data_dir):
    put(vault, "00_inbox/memo.md", TEXT.encode())
    run_apply(vault, data_dir, {})
    original = vault.root / "20_projects" / "A社" / "01_見積" / "memo.md"
    original.write_text(TEXT + "追記した本文です。", encoding="utf-8")
    (item,) = prepare(vault, data_dir, NOW + timedelta(minutes=1))["items"]
    assert (item["kind"], item["name"]) == ("changed", "memo.md")
    original.unlink()
    result = prepare(vault, data_dir, NOW + timedelta(minutes=2))
    expected = "**Removed** .notes/20_projects/A社/01_見積/memo.md.md（原本が見つからない）"
    assert result["done"] == [expected]
    assert not vault.note_path(original).exists()


def test_prepareは項目がなければ索引を作り直してロックを解く(vault, data_dir):
    prepare(vault, data_dir, NOW)
    assert (data_dir / "reconcile.lock").exists() is False
    assert (vault.root / "index.md").exists()


def test_prepareのwarningsは予約名の案件フォルダがなければ空(vault, data_dir):
    (vault.root / "20_projects" / "A" / "01_x").mkdir(parents=True)

    assert prepare(vault, data_dir, NOW)["warnings"] == []


@pytest.mark.parametrize(
    ("rel", "reserved"),
    [("20_projects/knowledge", "40_knowledge"), ("80_archive/Shared", "30_shared")],
    ids=["knowledge", "大文字小文字違いのShared"],
)
def test_prepareのwarningsは予約名の案件フォルダを1件ずつ警告する(vault, data_dir, rel, reserved):
    (vault.root / rel / "01_x").mkdir(parents=True)

    warnings = prepare(vault, data_dir, NOW)["warnings"]

    assert len(warnings) == 1
    assert warnings[0].startswith(f"{rel} は予約名の案件で、{reserved} と衝突します。")
    assert f"{reserved}/<区分>" in warnings[0]


def test_prepareのwarningsは20_projectsと80_archiveの両方にあれば2件出す(vault, data_dir):
    (vault.root / "20_projects" / "knowledge" / "01_x").mkdir(parents=True)
    (vault.root / "80_archive" / "shared" / "01_x").mkdir(parents=True)

    warnings = prepare(vault, data_dir, NOW)["warnings"]

    assert [w.split(" ")[0] for w in warnings] == ["20_projects/knowledge", "80_archive/shared"]


def test_prepareのwarningsはフォルダでない名前を警告しない(vault, data_dir):
    (vault.root / "20_projects" / "shared").write_bytes(b"file")  # ファイル

    assert prepare(vault, data_dir, NOW)["warnings"] == []


def test_prepareのcontextに案件ごとの区分の件数と番号なし名の合計と取引先が入る(vault, data_dir):
    register(vault, "A", "01_x", "f1.txt")
    (vault.root / "20_projects" / "A" / "02_y").mkdir()
    register(vault, "B", "03_x", "f2.txt")
    make_note(vault, "A", "01_x", "f1.txt", client="甲社")
    make_note(vault, "B", "03_x", "f2.txt", client="乙社")
    context = prepare(vault, data_dir, NOW)["context"]
    assert context["projects"] == {
        "A": {"01_x": 1, "02_y": 0},
        "B": {"03_x": 1},
        "shared": {"99_その他": 0},
        "knowledge": {"99_その他": 0},
    }
    assert context["category_names"] == {"x": 2, "y": 0, "その他": 0}
    assert context["clients"] == ["甲社", "乙社"]


def test_prepare後の束はOKFの形を満たす(vault, data_dir):
    moved = register(vault, "A", "01_x", "f.txt")
    gone = register(vault, "A", "01_x", "g.txt", "消える資料の本文です。二十字を超える長さ。")
    target = vault.root / "20_projects" / "B" / "02_y"
    target.mkdir(parents=True)
    moved.rename(target / "f.txt")
    gone.unlink()
    result = prepare(vault, data_dir, NOW)
    assert len(result["done"]) == 2
    assert check_bundle(vault.root) == []


@pytest.mark.parametrize(
    ("rel", "content", "expected"),
    [
        ("20_projects/A/01_x/n.md", "本文だけ\n", "frontmatter がない"),
        ("20_projects/A/01_x/n.md", '---\ntype: ""\n---\n', "type が空"),
        ("20_projects/A/index.md", "---\ntype: x\n---\n", "index.md に frontmatter がある"),
        ("log.md", "## 2026-10-30\n\n## 2026-10-31\n", "降順でない"),
    ],
    ids=["frontmatterなし", "type空", "索引にfrontmatter", "ログが昇順"],
)
def test_check_bundleは違反を文で返す(vault, rel, content, expected):
    put(vault, rel, content.encode())
    problems = check_bundle(vault.root)
    assert len(problems) == 1
    assert expected in problems[0]


def test_prepareは原本を改名してもノートを消さずresourceを直す(vault, data_dir):
    original = register(vault, "A", "01_x", "f.txt")
    original.rename(original.parent / "g.txt")
    result = prepare(vault, data_dir, NOW)
    assert result["items"] == []
    assert result["done"] == [
        "**Followed** .notes/20_projects/A/01_x/f.txt.md → .notes/20_projects/A/01_x/g.txt.md"
    ]
    meta = read_frontmatter(vault.notes_dir / "20_projects" / "A" / "01_x" / "g.txt.md")
    assert meta["resource"] == "20_projects/A/01_x/g.txt"
    assert "**Followed**" in (vault.root / "log.md").read_text(encoding="utf-8")


def test_prepareは追従で移すとき利用者が足したプロパティを残す(vault, data_dir):
    original = register(vault, "A", "01_x", "f.txt")
    make_note(vault, "A", "01_x", "f.txt", aliases=["x"], **{"sha256": convert.sha256_of(original)})
    target = vault.root / "20_projects" / "B" / "02_y"
    target.mkdir(parents=True)
    original.rename(target / "f.txt")
    prepare(vault, data_dir, NOW)
    assert read_frontmatter(vault.notes_dir / "20_projects" / "B" / "02_y" / "f.txt.md")[
        "aliases"
    ] == ["x"]


def test_prepareはノートの移動に失敗してもerrorsに入れて他を続けログを残す(vault, data_dir):
    first = register(vault, "A", "01_x", "f.txt")
    second = register(vault, "A", "01_x", "g.txt", "別の資料の本文です。二十字を超える長さ。")
    target = vault.root / "20_projects" / "B" / "02_y"
    target.mkdir(parents=True)
    first.rename(target / "f.txt")
    second.rename(target / "g.txt")
    with (vault.notes_dir / "20_projects" / "A" / "01_x" / "f.txt.md").open(
        "rb"
    ):  # 開いたままのノートは移せない
        result = prepare(vault, data_dir, NOW)
    assert result["items"] == []
    [error] = result["errors"]
    assert error["source"] == ".notes/20_projects/A/01_x/f.txt.md"
    assert "WinError 32" in error["message"]
    assert result["done"] == [
        "**Followed** .notes/20_projects/A/01_x/g.txt.md → .notes/20_projects/B/02_y/g.txt.md"
    ]
    assert (vault.notes_dir / "20_projects" / "A" / "01_x" / "f.txt.md").exists()
    assert "g.txt.md" in (vault.root / "log.md").read_text(encoding="utf-8")


def test_prepareは孤児の照合中に原本が開けなければ孤児を消さず_errorsは1回だけ(vault, data_dir):
    original = register(vault, "A", "01_x", "f.txt")
    original.rename(original.parent / "g.txt")
    with locked(original.parent / "g.txt"):
        result = prepare(vault, data_dir, NOW)
    assert vault.note_path(original).exists()
    assert result["done"] == []
    assert result["errors"] == [
        {"source": "20_projects/A/01_x/g.txt", "message": "[Errno 13] Permission denied"}
    ]


def test_prepareが例外で落ちたらロックを解きそれまでのログは残す(vault, data_dir, monkeypatch):
    original = register(vault, "A", "01_x", "f.txt")
    original.unlink()
    make_txt(vault.root / "00_inbox" / "x.txt", TEXT)

    def interrupted(self, source, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(MarkItDown, "convert", interrupted)  # 変換中に Ctrl+C
    with pytest.raises(KeyboardInterrupt):
        prepare(vault, data_dir, NOW)
    assert not (data_dir / "reconcile.lock").exists()
    assert "**Removed**" in (vault.root / "log.md").read_text(encoding="utf-8")


def test_prepareはドットで始まるmdをIgnoredにしない(vault, data_dir):
    put(vault, "00_inbox/.hidden.md", b"x")
    assert prepare(vault, data_dir, NOW)["done"] == []


def test_check_bundleはCRLFのノートも正しいと判定する(vault):
    text = (
        "---"
        + chr(13)
        + chr(10)
        + "type: document"
        + chr(13)
        + chr(10)
        + "---"
        + chr(13)
        + chr(10)
        + "本文"
        + chr(13)
        + chr(10)
    )
    put(vault, "20_projects/A/01_x/n.md", text.encode())
    assert check_bundle(vault.root) == []


def plan_item(source, **override):
    item = {
        "source": source,
        "kind": "inbox",
        "project": "A社",
        "category": "見積",
        "title": "見積書",
        "description": "見積の説明",
        "tags": ["見積"],
        "client": "A社",
        "doc_type": "見積書",
        "date": "2026-10-15",
    }
    return item | override


def run_apply(vault, data_dir, *overrides, now=NOW):
    """prepare の項目を順に計画へ写し（overrides で上書き）、apply の結果を返す。"""
    items = prepare(vault, data_dir, now)["items"]
    plan = {
        "items": [
            plan_item(i["source"], kind=i["kind"], **o)
            for i, o in zip(items, overrides, strict=True)
        ]
    }
    return apply(vault, data_dir, plan, now)


def test_applyはinboxの資料を案件区分へ名前のまま移し隣にノートを書く(vault, data_dir):
    make_txt(vault.root / "00_inbox" / "x.txt", TEXT)
    result = run_apply(vault, data_dir, {})
    assert result["results"] == [
        {
            "source": "00_inbox/x.txt",
            "status": "filed",
            "note": ".notes/20_projects/A社/01_見積/x.txt.md",
            "message": None,
            "original": str(vault.root / "20_projects" / "A社" / "01_見積" / "x.txt"),
        }
    ]
    folder = vault.root / "20_projects" / "A社" / "01_見積"
    note = vault.notes_dir / "20_projects" / "A社" / "01_見積" / "x.txt.md"
    assert (folder / "x.txt").read_text(encoding="utf-8") == TEXT
    meta = read_frontmatter(note)
    assert (meta["resource"], meta["category"], meta["project"]) == (
        "20_projects/A社/01_見積/x.txt",
        "01_見積",
        "A社",
    )
    assert (meta["title"], meta["client"], meta["date"], meta["readable"]) == (
        "見積書",
        "A社",
        "2026-10-15",
        True,
    )
    assert TEXT in note.read_text(encoding="utf-8")
    assert [p.name for p in folder.iterdir()] == ["x.txt"]  # 区分フォルダには原本だけ
    assert not (vault.root / "00_inbox" / "x.txt").exists()
    assert not (data_dir / "reconcile.lock").exists()
    assert list((data_dir / "staging").iterdir()) == []
    src, dst = (
        vault.root / "00_inbox" / "x.txt",
        vault.root / "20_projects" / "A社" / "01_見積" / "x.txt",
    )
    assert result["done"] == [
        f"**Filed** x.txt → A社/01_見積（見積書、A社、2026-10-15） — 元: {src} / 先: {dst}"
    ]
    assert check_bundle(vault.root) == []


def test_applyは区分を番号なしで受け取り既存に寄せ_なければ次の番号で新設する(vault, data_dir):
    (vault.root / "20_projects" / "A社" / "01_見積").mkdir(parents=True)
    make_txt(vault.root / "00_inbox" / "a.txt", TEXT)
    make_txt(vault.root / "00_inbox" / "b.txt", TEXT + "別")
    run_apply(vault, data_dir, {"category": "見積"}, {"category": "稟議"})
    assert (vault.root / "20_projects" / "A社" / "01_見積" / "a.txt").exists()
    assert (vault.root / "20_projects" / "A社" / "02_稟議" / "b.txt").exists()


def test_applyは案件名を既存の案件フォルダに寄せる(vault, data_dir):
    (vault.root / "20_projects" / "A社" / "01_見積").mkdir(parents=True)
    make_txt(vault.root / "00_inbox" / "a.txt", TEXT)
    run_apply(vault, data_dir, {"project": "a社"})
    assert (vault.root / "20_projects" / "A社" / "01_見積" / "a.txt").exists()


def test_applyは案件直下のinboxを同じ案件の区分へ移す(vault, data_dir):
    (vault.root / "20_projects" / "B社").mkdir()
    make_txt(vault.root / "20_projects" / "B社" / "x.txt", TEXT)
    result = run_apply(vault, data_dir, {"project": "B社", "category": "契約"})
    assert result["results"][0]["status"] == "filed"
    assert (vault.root / "20_projects" / "B社" / "01_契約" / "x.txt").exists()
    assert not (vault.root / "20_projects" / "B社" / "x.txt").exists()


def test_applyは同名で中身が同じならinbox側を消しDiscardedを書く(vault, data_dir):
    register(vault, "A社", "01_見積", "x.txt")
    make_txt(vault.root / "00_inbox" / "x.txt", TEXT)
    result = run_apply(vault, data_dir, {})
    assert result["results"] == [
        {
            "source": "00_inbox/x.txt",
            "status": "discarded",
            "note": None,
            "message": None,
            "original": str(vault.root / "20_projects" / "A社" / "01_見積" / "x.txt"),
        }
    ]
    src, existing = (
        vault.root / "00_inbox" / "x.txt",
        vault.root / "20_projects" / "A社" / "01_見積" / "x.txt",
    )
    assert result["done"] == [
        f"**Discarded** x.txt（A社/01_見積 に同じ中身） — 元: {src} / 既存: {existing}"
    ]
    assert not (vault.root / "00_inbox" / "x.txt").exists()
    assert [p.name for p in (vault.root / "20_projects" / "A社" / "01_見積").iterdir()] == ["x.txt"]
    assert [p.name for p in (vault.notes_dir / "20_projects" / "A社" / "01_見積").iterdir()] == [
        "index.md",
        "x.txt.md",
    ]


def test_applyは同名で中身が違えばconflictを返し動かさず記録も作らない(vault, data_dir):
    existing = register(vault, "A社", "01_見積", "x.txt")
    make_txt(vault.root / "00_inbox" / "x.txt", TEXT + "別")
    result = run_apply(vault, data_dir, {})
    assert result["results"] == [
        {
            "source": "00_inbox/x.txt",
            "status": "conflict",
            "note": None,
            "message": None,
            "original": str(existing),
        }
    ]
    assert (vault.root / "00_inbox" / "x.txt").exists()
    assert existing.read_text(encoding="utf-8") == TEXT
    assert not (vault.root / ".vault-kb-conflicts.json").exists()


def test_旧版の保留の記録が残っていても無視され_inboxの資料は毎回prepareに出る(vault, data_dir):
    make_txt(vault.root / "00_inbox" / "x.txt", TEXT)
    (vault.root / ".vault-kb-conflicts.json").write_text(
        '[{"source": "00_inbox/x.txt", "destination": "...", "size": 1, "mtime_ns": 1}]',
        encoding="utf-8",
    )
    for _ in range(2):
        result = prepare(vault, data_dir, NOW)
        assert [item["source"] for item in result["items"]] == ["00_inbox/x.txt"]


def test_applyはunregisteredの原本を動かさずノートだけ書く(vault, data_dir):
    folder = vault.root / "20_projects" / "A社" / "01_見積"
    folder.mkdir(parents=True)
    make_txt(folder / "f.txt", TEXT)
    before = (folder / "f.txt").read_bytes()
    result = run_apply(vault, data_dir, {"project": "無視される", "category": "無視される"})
    assert result["results"][0]["status"] == "filed"
    assert result["results"][0]["note"] == ".notes/20_projects/A社/01_見積/f.txt.md"
    assert (folder / "f.txt").read_bytes() == before
    meta = read_frontmatter(vault.note_path(folder / "f.txt"))
    assert (meta["project"], meta["category"], meta["sha256"]) == (
        "A社",
        "01_見積",
        convert.sha256_of(folder / "f.txt"),
    )
    assert result["done"] == [
        "**Filed** f.txt → A社/01_見積（見積書、A社、2026-10-15）"
        f" — 元: {folder / 'f.txt'} / 先: {folder / 'f.txt'}"
    ]
    assert not (vault.root / "無視される").exists()
    assert check_bundle(vault.root) == []


def test_applyはchangedのノートを新しい本文と項目で書き直しRefreshedを書く(vault, data_dir):
    original = register(vault, "A社", "01_見積", "f.txt")
    make_note(
        vault, "A社", "01_見積", "f.txt", aliases=["別名"], sha256=convert.sha256_of(original)
    )
    new_text = "書き換えた後の本文です。二十字を超える長さにしてあります。"
    make_txt(original, new_text)
    result = run_apply(vault, data_dir, {"title": "新しい題", "date": None})
    assert result["results"] == [
        {
            "source": "20_projects/A社/01_見積/f.txt",
            "status": "refreshed",
            "note": ".notes/20_projects/A社/01_見積/f.txt.md",
            "message": None,
            "original": str(original),
        }
    ]
    assert result["done"] == ["**Refreshed** .notes/20_projects/A社/01_見積/f.txt.md"]
    note = vault.note_path(original)
    meta = read_frontmatter(note)
    assert (meta["title"], meta["date"], meta["aliases"]) == ("新しい題", None, ["別名"])
    assert meta["sha256"] == convert.sha256_of(original)
    text = note.read_text(encoding="utf-8")
    assert new_text in text
    assert "\n本文\n" not in text


def test_applyは取引先を既存の表記に寄せる(vault, data_dir):
    original = register(vault, "A社", "01_見積", "f.txt")
    make_note(vault, "A社", "01_見積", "f.txt", client="A社", sha256=convert.sha256_of(original))
    make_txt(
        vault.root / "00_inbox" / "x.txt", "別の資料の本文です。二十字を超える長さにしてあります。"
    )
    run_apply(vault, data_dir, {"client": "Ａ社"})
    note = vault.notes_dir / "20_projects" / "A社" / "01_見積" / "x.txt.md"
    assert read_frontmatter(note)["client"] == "A社"


def test_applyはreadable_falseの資料も置き_本文は空で説明に理由がある(vault, data_dir):
    (vault.root / "00_inbox" / "scan.zip").write_bytes(b"PK\x03\x04dummy")
    result = run_apply(
        vault,
        data_dir,
        {
            "project": "shared",
            "category": "99_その他",
            "description": "文字を取り出せない圧縮ファイル",
        },
    )
    assert result["results"][0]["status"] == "filed"
    note = vault.notes_dir / "30_shared" / "99_その他" / "scan.zip.md"
    meta = read_frontmatter(note)
    assert meta["readable"] is False
    assert meta["description"] == "文字を取り出せない圧縮ファイル"
    assert note.read_text(encoding="utf-8").endswith("---\n\n\n")
    assert check_bundle(vault.root) == []


def test_移せないファイルはその1件だけerrorにして続ける(vault, data_dir):
    make_txt(vault.root / "00_inbox" / "a.txt", TEXT)
    make_txt(vault.root / "00_inbox" / "b.txt", TEXT + "別")
    with (vault.root / "00_inbox" / "a.txt").open("rb"):  # 開いたままのファイルは移せない
        result = run_apply(vault, data_dir, {}, {})
    assert {r["source"]: r["status"] for r in result["results"]} == {
        "00_inbox/a.txt": "error",
        "00_inbox/b.txt": "filed",
    }
    [message] = [r["message"] for r in result["results"] if r["status"] == "error"]
    assert "WinError 32" in message
    assert (vault.root / "00_inbox" / "a.txt").exists()
    assert not (vault.notes_dir / "20_projects" / "A社" / "01_見積" / "a.txt.md").exists()
    assert (vault.root / "index.md").exists()
    assert not (data_dir / "reconcile.lock").exists()
    src, dst = (
        vault.root / "00_inbox" / "b.txt",
        vault.root / "20_projects" / "A社" / "01_見積" / "b.txt",
    )
    assert result["done"] == [
        f"**Filed** b.txt → A社/01_見積（見積書、A社、2026-10-15） — 元: {src} / 先: {dst}"
    ]


def test_applyはdateのない項目も片づける(vault, data_dir):
    make_txt(vault.root / "00_inbox" / "x.txt", TEXT)
    items = prepare(vault, data_dir, NOW)["items"]
    item = plan_item(items[0]["source"])
    del item["date"]

    (result,) = apply(vault, data_dir, {"items": [item]}, NOW)["results"]

    assert result["status"] == "filed"
    assert read_frontmatter(vault.root / result["note"])["date"] is None


def test_ノートの絶対パスが259文字を超える資料は動かさずerror(vault, data_dir):
    make_txt(vault.root / "00_inbox" / "x.txt", TEXT)
    # 原本の絶対パスは 254 文字（259 以下）。ノートは .notes/ と .md の分で 264 文字になる
    fixed = len(str(vault.root)) + len("/20_projects//01_見積/x.txt") + 1
    project = "a" * (255 - fixed)
    (item,) = run_apply(vault, data_dir, {"project": project, "category": "01_見積"})["results"]
    assert item["status"] == "error"
    assert "259" in item["message"]
    assert (vault.root / "00_inbox" / "x.txt").exists()


def test_prepareは件数の上限で打ち切り_残りをmoreで返し_次のprepareで続きを返す(vault, data_dir):
    for i in range(25):
        make_txt(vault.root / "00_inbox" / f"{i:02d}.txt", f"{TEXT}{i}")
    first = prepare(vault, data_dir, NOW)
    assert (len(first["items"]), first["more"]) == (MAX_ITEMS, 25 - MAX_ITEMS)
    plan = {"items": [plan_item(i["source"], kind=i["kind"]) for i in first["items"]]}
    assert {r["status"] for r in apply(vault, data_dir, plan, NOW)["results"]} == {"filed"}

    second = prepare(vault, data_dir, NOW)

    assert (len(second["items"]), second["more"]) == (25 - MAX_ITEMS, 0)


def test_prepareは変換に時間がかかったら打ち切り_残りをmoreで返す(vault, data_dir, monkeypatch):
    for i in range(5):
        make_txt(vault.root / "00_inbox" / f"{i:02d}.txt", f"{TEXT}{i}")
    ticks = iter(range(0, 10_000, 40))  # 変換 1 件ごとに 40 秒進む時計（2 件目で 60 秒を超える）
    monkeypatch.setattr(reconcile, "time", SimpleNamespace(monotonic=lambda: next(ticks)))

    result = prepare(vault, data_dir, NOW)

    assert (len(result["items"]), result["more"]) == (2, 3)


def test_prepareは時間切れでも最低1件は返す(vault, data_dir, monkeypatch):
    make_txt(vault.root / "00_inbox" / "a.txt", TEXT)
    make_txt(vault.root / "00_inbox" / "b.txt", f"{TEXT}b")
    ticks = iter(range(1_000, 100_000, 1_000))  # 最初の 1 件の前から、すでに時間切れ
    monkeypatch.setattr(reconcile, "time", SimpleNamespace(monotonic=lambda: next(ticks)))

    result = prepare(vault, data_dir, NOW)

    assert (len(result["items"]), result["more"]) == (1, 1)


def test_prepareは原本が揃っていない疑いがあれば_ノートを消さずerrorで知らせる(vault, data_dir):
    notes = [make_note(vault, "A", "01_x", f"{i}.txt") for i in range(6)]  # 原本が 1 つもない

    result = prepare(vault, data_dir, NOW)

    assert all(note.exists() for note in notes)
    assert result["done"] == []
    assert [e["source"] for e in result["errors"]] == [".notes"]
    assert "6 件" in result["errors"][0]["message"]


def test_prepareは原本のないノートが半数以下なら消す(vault, data_dir):
    notes = [make_note(vault, "A", "01_x", f"{i}.txt") for i in range(6)]
    for i in range(6):
        register(vault, "A", "01_y", f"live{i}.txt")

    result = prepare(vault, data_dir, NOW)

    assert not any(note.exists() for note in notes)
    assert len(result["done"]) == 6
    assert result["errors"] == []


def test_prepareは原本のないノートが5件以下なら全部でも消す(vault, data_dir):
    notes = [make_note(vault, "A", "01_x", f"{i}.txt") for i in range(5)]

    result = prepare(vault, data_dir, NOW)

    assert not any(note.exists() for note in notes)
    assert len(result["done"]) == 5


def test_prepareは開けない資料が上限の枠を使わない(vault, data_dir):
    for i in range(MAX_ITEMS + 1):
        make_txt(vault.root / "00_inbox" / f"{i:02d}.txt", f"{TEXT}{i}")
    with locked(vault.root / "00_inbox" / "00.txt"):
        result = prepare(vault, data_dir, NOW)
    assert (len(result["items"]), result["more"]) == (MAX_ITEMS, 0)
    assert [e["source"] for e in result["errors"]] == ["00_inbox/00.txt"]


def test_移動先の絶対パスが259文字を超える資料はerror(vault, data_dir):
    make_txt(vault.root / "00_inbox" / "x.txt", TEXT)
    result = run_apply(vault, data_dir, {"project": "あ" * 235})
    (item,) = result["results"]
    assert item["status"] == "error"
    assert "259" in item["message"]
    assert (vault.root / "00_inbox" / "x.txt").exists()


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"project": "A社."}, "末尾"),
        ({"project": "A/社"}, "使えない文字"),
        ({"category": "99x_a."}, "末尾"),
        ({"category": "99_稟議"}, "98"),
        ({"project": "index"}, "予約語"),
        ({"project": "AUX"}, "予約名"),
    ],
    ids=["案件末尾ドット", "案件に区切り", "区分末尾ドット", "番号99", "予約語", "Windowsの予約名"],
)
def test_不正なフォルダ名はその項目だけerrorで原本は動かない(vault, data_dir, override, expected):
    make_txt(vault.root / "00_inbox" / "x.txt", TEXT)
    (item,) = run_apply(vault, data_dir, override)["results"]
    assert item["status"] == "error"
    assert expected in item["message"]
    assert (vault.root / "00_inbox" / "x.txt").exists()


def test_applyはノートでないmdがあれば上書きせず原本も移さない(vault, data_dir):
    folder = vault.root / "20_projects" / "A社" / "01_見積"
    folder.mkdir(parents=True)
    memo = put(vault, ".notes/20_projects/A社/01_見積/x.txt.md", "利用者のメモ".encode())
    make_txt(vault.root / "00_inbox" / "x.txt", TEXT)
    (item,) = run_apply(vault, data_dir, {})["results"]
    assert item["status"] == "error"
    assert item["message"] == ".notes/20_projects/A社/01_見積/x.txt.md に別のファイルがあります"
    assert memo.read_text(encoding="utf-8") == "利用者のメモ"
    assert (vault.root / "00_inbox" / "x.txt").exists()
    assert not (folder / "x.txt").exists()


def test_applyは不正な計画をValueErrorで拒み何も実行しない(vault, data_dir):
    make_txt(vault.root / "00_inbox" / "x.txt", TEXT)
    prepare(vault, data_dir, NOW)
    plan = {"items": [plan_item("00_inbox/x.txt", date="2026/10/15")]}
    with pytest.raises(ValueError, match="date"):
        apply(vault, data_dir, plan, NOW)
    assert (vault.root / "00_inbox" / "x.txt").exists()
    assert not (vault.root / "20_projects" / "A社").exists()


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        (
            {"source": "x.txt"},
            "source は 00_inbox・20_projects・80_archive・30_shared・40_knowledge の下",
        ),
        (
            {"source": "10_daily/x.txt"},
            "source は 00_inbox・20_projects・80_archive・30_shared・40_knowledge の下",
        ),
        ({"kind": "move"}, "kind が不正"),
        ({"tags": "見積"}, "tags は文字列の配列"),
        ({"title": None}, "title は文字列"),
        ({"category": ""}, "inbox の category は空でない"),
    ],
    ids=[
        "sourceが直下",
        "sourceが利用者の領域",
        "kind不正",
        "tagsが文字列",
        "titleがNone",
        "inboxの区分が空",
    ],
)
def test_applyは項目の形が不正なら全体を拒む(vault, data_dir, override, expected):
    make_txt(vault.root / "00_inbox" / "x.txt", TEXT)
    prepare(vault, data_dir, NOW)
    plan = {"items": [plan_item("00_inbox/x.txt") | override]}
    with pytest.raises(ValueError, match=expected):
        apply(vault, data_dir, plan, NOW)
    assert (vault.root / "00_inbox" / "x.txt").exists()


def test_applyの後の束はOKFに適合し_log_mdは新しい順(vault, data_dir):
    make_txt(vault.root / "00_inbox" / "a.txt", TEXT)
    run_apply(vault, data_dir, {})
    make_txt(vault.root / "00_inbox" / "b.txt", TEXT + "別")
    run_apply(vault, data_dir, {}, now=NOW + timedelta(days=1))
    assert check_bundle(vault.root) == []
    log = (vault.root / "log.md").read_text(encoding="utf-8")
    assert log.index("2026-11-01") < log.index("2026-10-31")
    assert log.index("**Filed** b.txt") < log.index("**Filed** a.txt")


def test_どの操作でも原本の名前と中身が変わらない(vault, data_dir):
    inbox = vault.root / "00_inbox" / "in.txt"
    make_txt(inbox, TEXT)
    moved = register(
        vault, "B社", "01_x", "f.txt", "追従される資料の本文です。二十字を超える長さ。"
    )
    kept = moved.read_bytes()
    target = vault.root / "20_projects" / "C社" / "02_y"
    target.mkdir(parents=True)
    moved.rename(target / "f.txt")
    changed = register(vault, "D社", "01_x", "g.txt", "書き換え前の本文です。二十字を超える長さ。")
    make_txt(changed, "書き換え後の本文です。二十字を超える長さにしてあります。")
    after = changed.read_bytes()
    original = inbox.read_bytes()
    run_apply(vault, data_dir, {}, {})
    assert (vault.root / "20_projects" / "A社" / "01_見積" / "in.txt").read_bytes() == original
    assert (target / "f.txt").read_bytes() == kept
    assert changed.read_bytes() == after
    for original_path in (target / "f.txt", changed):
        note = vault.note_path(original_path)
        assert read_frontmatter(note)["sha256"] == convert.sha256_of(original_path)


def apply_one(vault, data_dir, source, **override):
    """prepare を通さず 1 項目の計画を実行し、その結果を返す。"""
    return apply(vault, data_dir, {"items": [plan_item(source, **override)]}, NOW)["results"][0]


def test_区分内の原本をinboxで同じ区分に指定してもerrorで原本は残る(vault, data_dir):
    original = register(vault, "A社", "01_見積", "x.txt")
    item = apply_one(vault, data_dir, "20_projects/A社/01_見積/x.txt")
    assert item["status"] == "error"
    assert original.read_text(encoding="utf-8") == TEXT
    assert vault.note_path(original).exists()


def test_登録済みの原本をunregisteredで指定してもerrorでノートは残る(vault, data_dir):
    original = register(vault, "A社", "01_見積", "x.txt")
    make_note(vault, "A社", "01_見積", "x.txt", aliases=["別名"])
    item = apply_one(vault, data_dir, "20_projects/A社/01_見積/x.txt", kind="unregistered")
    assert item["status"] == "error"
    assert "登録済み" in item["message"]
    assert read_frontmatter(vault.note_path(original))["aliases"] == ["別名"]


def test_changedでノートでないmdがあればerrorで上書きしない(vault, data_dir):
    folder = vault.root / "20_projects" / "A社" / "01_見積"
    folder.mkdir(parents=True)
    make_txt(folder / "x.txt", TEXT)
    memo_text = "---\nfoo: 1\n---\n利用者の本文\n"
    memo = put(vault, ".notes/20_projects/A社/01_見積/x.txt.md", memo_text.encode())
    item = apply_one(vault, data_dir, "20_projects/A社/01_見積/x.txt", kind="changed")
    assert item["status"] == "error"
    assert "利用者の本文" in memo.read_text(encoding="utf-8")


def test_vaultの外やlog_mdを指すsourceはerrorで何も動かない(vault, data_dir):
    secret = vault.root.parent / "secret.txt"  # vault の外
    make_txt(secret, TEXT)
    log = vault.root / "log.md"
    log.write_text("## 2026-10-30\n", encoding="utf-8")
    outside = apply_one(vault, data_dir, "00_inbox/..\\..\\secret.txt")
    reserved = apply_one(vault, data_dir, "00_inbox/log.md")
    assert (outside["status"], reserved["status"]) == ("error", "error")
    assert secret.read_text(encoding="utf-8") == TEXT
    assert not (vault.root / "20_projects" / "A社").exists()


def test_新しい案件名がドットで始まればerror(vault, data_dir):
    make_txt(vault.root / "00_inbox" / "x.txt", TEXT)
    item = apply_one(vault, data_dir, "00_inbox/x.txt", project=".隠し")
    assert item["status"] == "error"
    assert (vault.root / "00_inbox" / "x.txt").exists()


@pytest.mark.parametrize(
    "plan", [[], {"items": "x"}], ids=["planがdictでない", "itemsがlistでない"]
)
def test_applyはplanの形が不正ならValueError(vault, data_dir, plan):
    with pytest.raises(ValueError, match="items"):
        apply(vault, data_dir, plan, NOW)


def test_索引の書き込みに失敗してもログとロック解除は行われ例外は出る(vault, data_dir):
    make_txt(vault.root / "00_inbox" / "x.txt", TEXT)
    blocker = vault.notes_dir / "20_projects" / "A社" / "01_見積" / "index.md"
    blocker.mkdir(parents=True)  # 索引を書けない（中身があるので、片づけが消さない）
    (blocker / "x").write_text("x", encoding="utf-8")
    with pytest.raises(OSError, match=r"index\.md"):
        run_apply(vault, data_dir, {})
    assert "**Filed** x.txt" in (vault.root / "log.md").read_text(encoding="utf-8")
    assert not (data_dir / "reconcile.lock").exists()
    assert list((data_dir / "staging").iterdir()) == []


def test_prepareとapplyの間に原本が変わればerrorで原本は残りノートは書かない(vault, data_dir):
    make_txt(vault.root / "00_inbox" / "x.txt", TEXT)
    prepare(vault, data_dir, NOW)
    make_txt(vault.root / "00_inbox" / "x.txt", TEXT + "追記")
    (item,) = apply(vault, data_dir, {"items": [plan_item("00_inbox/x.txt")]}, NOW)["results"]
    assert item == {
        "source": "00_inbox/x.txt",
        "status": "error",
        "note": None,
        "message": MISSING,
        "original": None,
    }
    assert (vault.root / "00_inbox" / "x.txt").read_text(encoding="utf-8") == TEXT + "追記"
    assert not (vault.root / "20_projects" / "A社").exists()


def test_読めない資料はprepareの印で読めないと記録し_説明が空なら理由を書く(vault, data_dir):
    zipped = vault.root / "00_inbox" / "scan.zip"
    zipped.write_bytes(b"PK\x03\x04dummy")
    marker = data_dir / "staging" / f"{convert.sha256_of(zipped)}.unreadable"
    prepare(vault, data_dir, NOW)
    assert marker.read_text(encoding="utf-8") == "対応しない形式です"
    item = plan_item("00_inbox/scan.zip", project="shared", category="99_その他", description="")
    (result,) = apply(vault, data_dir, {"items": [item]}, NOW)["results"]
    assert result["status"] == "filed"
    meta = read_frontmatter(vault.notes_dir / "30_shared" / "99_その他" / "scan.zip.md")
    assert (meta["readable"], meta["description"]) == (False, "対応しない形式です")


def test_対応しない拡張子の原本を手で動かしてもノートが追従する(vault, data_dir):
    (vault.root / "00_inbox" / "s.zip").write_bytes(b"PK\x03\x04dummy")
    run_apply(vault, data_dir, {"project": "shared", "category": "99_その他"})
    target = vault.root / "30_shared" / "02_x"
    target.mkdir()
    (vault.root / "30_shared" / "99_その他" / "s.zip").rename(target / "s.zip")
    result = prepare(vault, data_dir, NOW)
    assert result["done"] == [
        "**Followed** .notes/30_shared/99_その他/s.zip.md → .notes/30_shared/02_x/s.zip.md"
    ]
    assert result["items"] == []
    meta = read_frontmatter(vault.notes_dir / "30_shared" / "02_x" / "s.zip.md")
    assert (meta["category"], meta["readable"]) == ("02_x", False)


@pytest.mark.parametrize("token", ["2026-10-31T12:00:00+00:00", 1])
def test_applyは旧版のlock_token付きの計画も受け付ける(vault, data_dir, token):
    make_txt(vault.root / "00_inbox" / "x.txt", TEXT)
    (item,) = prepare(vault, data_dir, NOW)["items"]

    result = apply(
        vault, data_dir, {"lock_token": token, "items": [plan_item(item["source"])]}, NOW
    )

    assert [r["status"] for r in result["results"]] == ["filed"]


def test_リンクの根も_存在するフォルダとして扱う(vault, data_dir, tmp_path):
    outside = tmp_path / "外"
    outside.mkdir()
    (outside / "A" / "01_x").mkdir(parents=True)
    make_txt(outside / "A" / "01_x" / "f.txt", TEXT)
    (vault.root / "20_projects").rmdir()
    junction(vault.root / "20_projects", outside)

    result = prepare(vault, data_dir, NOW)

    assert result["warnings"] == []
    assert [i["source"] for i in result["items"]] == ["20_projects/A/01_x/f.txt"]


def test_applyのsourceがvaultの外へ解決されればerror_リンクの検査を削っても残る(
    vault, data_dir, tmp_path
):
    outside = tmp_path / "外"
    outside.mkdir()
    (outside / "x.txt").write_text(TEXT, encoding="utf-8")
    junction(vault.root / "20_projects" / "L", outside)

    item = apply_one(vault, data_dir, "20_projects/L/x.txt", kind="unregistered")

    assert item["status"] == "error"
    assert "vault の外" in item["message"]
    assert (outside / "x.txt").exists()


def test_新しい案件には99_その他も作り_区分その他はそこに置く(vault, data_dir):
    make_txt(vault.root / "00_inbox" / "a.txt", TEXT)
    make_txt(vault.root / "00_inbox" / "b.txt", TEXT + "別")
    run_apply(
        vault,
        data_dir,
        {"project": "B社", "category": "その他"},
        {"project": "C社", "category": "見積"},
    )
    assert (vault.root / "20_projects" / "B社" / "99_その他" / "a.txt").exists()
    assert sorted(p.name for p in (vault.root / "20_projects" / "B社").iterdir()) == ["99_その他"]
    assert sorted(p.name for p in (vault.root / "20_projects" / "C社").iterdir()) == [
        "01_見積",
        "99_その他",
    ]
    assert (vault.notes_dir / "20_projects" / "C社" / "index.md").exists()


def test_区分の下のサブフォルダの原本は動かさずノートを作りログは案件と区分を示す(vault, data_dir):
    sub = vault.root / "20_projects" / "A社" / "01_見積" / "sub"
    sub.mkdir(parents=True)
    make_txt(sub / "f.txt", TEXT)
    result = run_apply(vault, data_dir, {})
    assert result["results"][0]["note"] == ".notes/20_projects/A社/01_見積/sub/f.txt.md"
    assert (sub / "f.txt").read_text(encoding="utf-8") == TEXT
    meta = read_frontmatter(
        vault.notes_dir / "20_projects" / "A社" / "01_見積" / "sub" / "f.txt.md"
    )
    assert (meta["project"], meta["category"]) == ("A社", "01_見積")
    assert result["done"] == [
        "**Filed** f.txt → A社/01_見積（見積書、A社、2026-10-15）"
        f" — 元: {sub / 'f.txt'} / 先: {sub / 'f.txt'}"
    ]
    index = (vault.notes_dir / "20_projects" / "A社" / "01_見積" / "index.md").read_text(
        encoding="utf-8"
    )
    assert "(<sub/f.txt.md>)" in index


def test_開いたままの資料はinboxに残り_作りかけのフォルダも残さない(vault, data_dir):
    path = vault.root / "00_inbox" / "x.txt"
    make_txt(path, TEXT)
    with path.open("rb"):
        (item,) = run_apply(vault, data_dir, {})["results"]
    assert item["status"] == "error"
    assert "WinError 32" in item["message"]
    assert path.exists()
    assert not (vault.root / "20_projects" / "A社").exists()


def test_取引先が空ならFiledの行に空の欄を書かない(vault, data_dir):
    make_txt(vault.root / "00_inbox" / "x.txt", TEXT)
    result = run_apply(vault, data_dir, {"client": ""})
    src, dst = (
        vault.root / "00_inbox" / "x.txt",
        vault.root / "20_projects" / "A社" / "01_見積" / "x.txt",
    )
    assert result["done"] == [
        f"**Filed** x.txt → A社/01_見積（見積書、2026-10-15） — 元: {src} / 先: {dst}"
    ]


def test_ドットで始まるフォルダの中のsourceはerrorで動かない(vault, data_dir):
    hidden = vault.root / "20_projects" / ".obsidian" / "x.txt"
    hidden.parent.mkdir(parents=True)
    make_txt(hidden, TEXT)
    item = apply_one(vault, data_dir, "20_projects/.obsidian/x.txt")
    assert item["status"] == "error"
    assert "資料として扱えない" in item["message"]
    assert hidden.exists()
    assert not (vault.root / "20_projects" / "A社").exists()


def test_applyが済むと_vault直下の計画ファイルが消える(vault, data_dir):
    plan_file = vault.root / PLAN_NAME
    plan_file.write_text("{}", encoding="utf-8")
    make_txt(vault.root / "00_inbox" / "x.txt", TEXT)

    run_apply(vault, data_dir, {})

    assert not plan_file.exists()


def test_applyの1件が失敗しても計画ファイルは消える(vault, data_dir):
    plan_file = vault.root / PLAN_NAME
    plan_file.write_text("{}", encoding="utf-8")
    make_txt(vault.root / "00_inbox" / "x.txt", TEXT)

    result = run_apply(vault, data_dir, {"project": "index"})  # 予約語の案件名は、その 1 件の失敗

    assert [r["status"] for r in result["results"]] == ["error"]
    assert not plan_file.exists()


def test_applyが計画の形の誤りで拒むときは計画ファイルを消さない(vault, data_dir):
    plan_file = vault.root / PLAN_NAME
    plan_file.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="items"):
        apply(vault, data_dir, {}, NOW)

    assert plan_file.exists()


def test_prepareは項目を返しても_ロックを残さない(vault, data_dir):
    make_txt(vault.root / "00_inbox" / "x.txt", TEXT)
    assert prepare(vault, data_dir, NOW)["items"]
    assert not (data_dir / "reconcile.lock").exists()


def test_applyは計画にないshaのstagingを消さない(vault, data_dir):
    make_txt(vault.root / "00_inbox" / "a.txt", TEXT)
    make_txt(vault.root / "00_inbox" / "b.txt", TEXT + "別の中身")
    items = prepare(vault, data_dir, NOW)["items"]
    assert len(items) == 2
    planned, other = items
    apply(vault, data_dir, {"items": [plan_item(planned["source"])]}, NOW)
    assert not Path(planned["text"]).exists()
    assert Path(other["text"]).exists()


def test_applyは同じ中身の2資料を同じstagingのまま両方filedにして後で消す(vault, data_dir):
    make_txt(vault.root / "00_inbox" / "a.txt", TEXT)
    make_txt(vault.root / "00_inbox" / "b.txt", TEXT)
    items = prepare(vault, data_dir, NOW)["items"]
    assert items[0]["text"] == items[1]["text"]
    plan = {"items": [plan_item(i["source"]) for i in items]}
    result = apply(vault, data_dir, plan, NOW)
    assert [r["status"] for r in result["results"]] == ["filed", "filed"]
    assert list((data_dir / "staging").iterdir()) == []
