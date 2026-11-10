"""migrate: 旧形式（2.2.0 まで）のノートと .wiki/ のページを、新しい形式へ一括で直す。"""

import json
import os
import stat
import sys
from datetime import UTC, datetime

import pytest
from helpers import make_note, put
from vault_kb import migrate
from vault_kb.bundle import read_frontmatter
from vault_kb.lock import acquire
from vault_kb.wiki import digest, project_entries

NOW = datetime(2026, 11, 10, 9, 30, 15, tzinfo=UTC)
P = "A社"
REL = f"20_projects/{P}/01_見積/a.pdf"
TS = "2026-10-31T00:00:00+09:00"


def lf(path, text):
    """LF のまま書く（Windows の Path.write_text は改行を CRLF にする。プラグインは LF で書く）。"""
    path.write_bytes(text.encode("utf-8"))


def legacy_note(vault, name="a.pdf", sha="a"):
    """2.2.0 までの形のノート（resource に先頭 / がなく、timestamp がある）を置く。"""
    path = make_note(vault, P, "01_見積", name, sha256=sha * 64, timestamp=TS)
    put(vault, f"20_projects/{P}/01_見積/{name}")
    lf(path, path.read_text(encoding="utf-8").replace('resource: "/', 'resource: "'))
    return path


def legacy_page(vault, rel, meta, body="本文"):
    path = vault.wiki_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    head = "".join(f"{k}: {json.dumps(v, ensure_ascii=False)}\n" for k, v in meta.items())
    lf(path, f"---\n{head}---\n\n{body}\n")
    return path


def legacy_vault(vault):
    legacy_note(vault, "a.pdf", "a")
    legacy_note(vault, "b.pdf", "b")
    sources = [
        {"resource": e["resource"], "sha256": e["sha256"]} for e in project_entries(vault, P)
    ]
    legacy_page(
        vault,
        f"projects/{P}.md",
        {
            "type": "synthesis",
            "project": P,
            "timestamp": TS,
            "sources_digest": digest(sources),
            "sources": sources,
            "unread": [],
        },
    )
    legacy_page(
        vault,
        "queries/q.md",
        {
            "type": "query",
            "question": "保守費は？",
            "timestamp": TS,
            "evidence": sources[:1],
            "scope": [P],
            "scope_digest": "x",
        },
        body="月額 20 万円",
    )


def tree(vault):
    return {p: p.read_bytes() for p in vault.root.rglob("*") if p.is_file()}


def test_dry_runは書き換える対象を数えて報告し何も書かない(vault, data_dir):
    legacy_vault(vault)
    before = tree(vault)

    result = migrate.run(vault, data_dir, NOW, apply=False)

    assert result["dry_run"] is True
    assert sorted(result["changed"]) == [
        f".notes/{REL}.md",
        f".notes/20_projects/{P}/01_見積/b.pdf.md",
        f".wiki/projects/{P}.md",
        ".wiki/queries/q.md",
    ]
    assert result["skipped"] == []
    assert result["backup"] is None
    assert tree(vault) == before
    assert not (data_dir / "backup").exists()


def test_applyは旧形式を新形式へ直し_本文は変えず_元のファイルをバックアップする(vault, data_dir):
    legacy_vault(vault)
    note = vault.notes_dir / f"{REL}.md"
    old_note = note.read_bytes()
    old_query = (vault.wiki_dir / "queries/q.md").read_bytes()

    result = migrate.run(vault, data_dir, NOW, apply=True)

    assert result["dry_run"] is False
    assert len(result["changed"]) == 4
    meta = read_frontmatter(note)
    assert f'resource: "/{REL}"' in note.read_text(encoding="utf-8")
    assert meta["resource"] == REL
    assert "timestamp" not in meta
    assert meta["generated"] == {"by": "vault-kb/legacy", "at": TS}  # 時刻は保ち、版は分からない
    query = read_frontmatter(vault.wiki_dir / "queries/q.md")
    assert "evidence" not in query
    assert query["sources"][0]["resource"] == REL
    assert query["title"] == "保守費は？"
    assert query["description"] == "質問「保守費は？」への回答（出典 1 件）"
    project = read_frontmatter(vault.wiki_dir / f"projects/{P}.md")
    assert (project["title"], project["description"]) == (P, f"{P}の統合ページ（ノート 2 件）")
    assert "月額 20 万円" in (vault.wiki_dir / "queries/q.md").read_text(encoding="utf-8")
    backup = data_dir / "backup" / "migrate-20261110-093015"
    assert result["backup"] == str(backup)
    assert (backup / f".notes/{REL}.md").read_bytes() == old_note
    assert (backup / ".wiki/queries/q.md").read_bytes() == old_query
    assert "保守費は？" in (vault.wiki_dir / "index.md").read_text(encoding="utf-8")
    assert not (data_dir / "reconcile.lock").exists()


def test_applyの後は何も直す対象がなく_もう一度実行しても変わらない(vault, data_dir):
    legacy_vault(vault)
    migrate.run(vault, data_dir, NOW, apply=True)
    after = tree(vault)

    again = migrate.run(vault, data_dir, NOW, apply=True)

    assert again["changed"] == []
    assert again["backup"] is None
    assert tree(vault) == after


def test_新形式だけのvaultでは何も直さない(vault, data_dir):
    make_note(vault, P, "01_見積", "a.pdf", generated={"by": "vault-kb/2.3.0", "at": TS})
    del_ts = vault.notes_dir / f"{REL}.md"
    kept = [
        line
        for line in del_ts.read_text(encoding="utf-8").splitlines(keepends=True)
        if not line.startswith("timestamp:")
    ]
    lf(del_ts, "".join(kept))
    put(vault, REL)

    assert migrate.run(vault, data_dir, NOW, apply=False)["changed"] == []


def test_1行1項目でないfrontmatterは壊さないよう触らず報告する(vault, data_dir):
    path = legacy_note(vault)
    text = path.read_text(encoding="utf-8")
    block = text.replace('tags: ["t"]', "tags:\n  - t")  # Obsidian が書き直した形
    path.write_text(block)

    result = migrate.run(vault, data_dir, NOW, apply=True)

    assert result["changed"] == []
    assert [s["path"] for s in result["skipped"]] == [f".notes/{REL}.md"]
    assert path.read_text(encoding="utf-8") == block


def test_applyはロックが取れなければbusyを返し何も書かない(vault, data_dir):
    legacy_vault(vault)
    before = tree(vault)
    acquire(data_dir, NOW)

    assert migrate.run(vault, data_dir, NOW, apply=True) == {"busy": True}

    assert tree(vault) == before
    assert not (data_dir / "backup").exists()


@pytest.mark.parametrize("apply", [False, True])
def test_indexのmdと予約名は対象にしない(vault, data_dir, apply):
    legacy_note(vault)
    lf(vault.notes_dir / "index.md", "# 目録\n")

    result = migrate.run(vault, data_dir, NOW, apply=apply)

    assert all(not p.endswith("index.md") for p in result["changed"])
    assert (vault.notes_dir / "index.md").read_text(encoding="utf-8") == "# 目録\n"


def body_of(path):
    """frontmatter の閉じ線より後ろの、バイト列のままの本文。"""
    raw = path.read_bytes()
    return raw[raw.index(b"\n---\n", 4) + len(b"\n---\n") :]


def test_本文は1文字も変えない_先頭のインデントや末尾の空白も残す(vault, data_dir):
    body = "    code\n  x\r\n\n\n\ttail   \n\n"
    page = legacy_page(vault, "queries/q.md", {"type": "query", "question": "質", "evidence": []})
    page.write_bytes(page.read_bytes().rsplit(b"---\n", 1)[0] + b"---\n" + body.encode("utf-8"))
    before = body_of(page)

    result = migrate.run(vault, data_dir, NOW, apply=True)

    assert result["changed"] == [".wiki/queries/q.md"]
    assert body_of(page) == before == body.encode("utf-8")


def test_UTF8で読めないmdがあっても止まらず_skippedに入れて他を直す(vault, data_dir):
    legacy_note(vault)
    bad = vault.notes_dir / "memo.md"
    bad.write_bytes(b"\x82\xa0\x82\xa2")

    dry = migrate.run(vault, data_dir, NOW, apply=False)

    assert dry["changed"] == [f".notes/{REL}.md"]
    assert dry["skipped"] == [{"path": ".notes/memo.md", "reason": "UTF-8 で読めない"}]
    assert migrate.run(vault, data_dir, NOW, apply=True)["changed"] == dry["changed"]
    assert bad.read_bytes() == b"\x82\xa0\x82\xa2"


def test_隠しフォルダ_bak_の旧いページは対象にしない(vault, data_dir):
    bak = legacy_page(
        vault, ".bak/project/旧.md", {"type": "synthesis", "project": "旧", "timestamp": TS}
    )
    before = bak.read_bytes()

    result = migrate.run(vault, data_dir, NOW, apply=True)

    assert result["changed"] == []
    assert bak.read_bytes() == before


def test_frontmatterのないメモや対象でないmdはskippedに数えない(vault, data_dir):
    vault.notes_dir.mkdir(exist_ok=True)
    lf(vault.notes_dir / "memo.md", "# メモ\n")
    legacy_page(vault, "memo2.md", {"type": "別の型", "title": "x"})  # ノートでもページでもない
    lf(vault.wiki_dir / "memo3.md", "---\ntags:\n  - a\n---\n本文\n")

    result = migrate.run(vault, data_dir, NOW, apply=False)

    assert (result["changed"], result["skipped"]) == ([], [])


def test_skippedがあっても他のファイルは直す(vault, data_dir):
    block = legacy_note(vault, "b.pdf", "b")
    lf(block, block.read_text(encoding="utf-8").replace('tags: ["t"]', "tags:\n  - t"))
    ok = legacy_note(vault, "a.pdf", "a")

    result = migrate.run(vault, data_dir, NOW, apply=True)

    assert result["changed"] == [f".notes/{REL}.md"]
    assert [s["path"] for s in result["skipped"]] == [f".notes/20_projects/{P}/01_見積/b.pdf.md"]
    assert 'resource: "/' in ok.read_text(encoding="utf-8")


def test_timestampが文字列でなければ消さずに残す(vault, data_dir):
    page = legacy_page(
        vault, "queries/q.md", {"type": "query", "question": "質", "timestamp": 12345}
    )

    migrate.run(vault, data_dir, NOW, apply=True)

    meta = read_frontmatter(page)
    assert meta["timestamp"] == 12345
    assert "generated" not in meta


def test_重複したキーのfrontmatterは値が落ちるので触らない(vault, data_dir):
    page = legacy_page(vault, "queries/q.md", {"type": "query", "question": "質"})
    text = page.read_text(encoding="utf-8")
    lf(page, text.replace('type: "query"\n', 'type: "query"\nquestion: "先"\n'))

    result = migrate.run(vault, data_dir, NOW, apply=True)

    assert result["changed"] == []
    assert [s["path"] for s in result["skipped"]] == [".wiki/queries/q.md"]


def test_シンボリックリンクのmdは読まず_書かない(vault, data_dir, tmp_path):
    outside = tmp_path / "外.md"
    lf(outside, legacy_note(vault).read_text(encoding="utf-8"))
    (vault.notes_dir / f"{REL}.md").unlink()
    link = vault.notes_dir / "link.md"
    try:
        os.symlink(outside, link)
    except OSError:
        pytest.skip("この環境ではシンボリックリンクを作れない")
    before = outside.read_bytes()

    result = migrate.run(vault, data_dir, NOW, apply=True)

    assert result["changed"] == []
    assert outside.read_bytes() == before


@pytest.mark.parametrize("stamp", ["2026-10-31T00:00:00", "いつか", 12345, None])
def test_オフセットのないtimestampは_generatedにせず_そのまま残す(vault, data_dir, stamp):
    page = legacy_page(
        vault, "queries/q.md", {"type": "query", "question": "質", "timestamp": stamp}
    )

    migrate.run(vault, data_dir, NOW, apply=True)

    meta = read_frontmatter(page)
    assert meta["timestamp"] == stamp
    assert "generated" not in meta  # OKF の generated.at は UTC オフセット必須


def test_generatedがすでにあれば_重なったtimestampを外す(vault, data_dir):
    page = legacy_page(
        vault,
        "queries/q.md",
        {"type": "query", "question": "質", "timestamp": TS, "generated": {"by": "x/1", "at": TS}},
    )

    migrate.run(vault, data_dir, NOW, apply=True)

    meta = read_frontmatter(page)
    assert "timestamp" not in meta
    assert meta["generated"] == {"by": "x/1", "at": TS}


def test_書く直前に内容が変わっていたら上書きせず_skippedにする(vault, data_dir, monkeypatch):
    note = legacy_note(vault)
    page = legacy_page(vault, "queries/q.md", {"type": "query", "question": "質", "evidence": []})
    planned = migrate._plan

    def edit_after_plan(v):  # 走査のあとに、利用者が保存した
        found = planned(v)
        lf(page, page.read_text(encoding="utf-8") + "利用者が追記\n")
        return found

    monkeypatch.setattr(migrate, "_plan", edit_after_plan)

    result = migrate.run(vault, data_dir, NOW, apply=True)

    assert result["changed"] == [f".notes/{REL}.md"]
    assert result["skipped"] == [
        {"path": ".wiki/queries/q.md", "reason": "確認のあとに内容が変わった"}
    ]
    assert "利用者が追記" in page.read_text(encoding="utf-8")
    assert 'resource: "/' in note.read_text(encoding="utf-8")


@pytest.mark.skipif(
    sys.platform != "win32", reason="読み取り専用への置き換えが失敗するのは Windows"
)
def test_1件の書き込みに失敗しても_他を直し_errorsとバックアップ先を返す(vault, data_dir):
    first = legacy_note(vault, "a.pdf", "a")
    locked = legacy_note(vault, "b.pdf", "b")
    last = legacy_note(vault, "c.pdf", "c")
    locked.chmod(stat.S_IREAD)
    try:
        result = migrate.run(vault, data_dir, NOW, apply=True)
    finally:
        locked.chmod(stat.S_IWRITE | stat.S_IREAD)

    assert [e["path"] for e in result["errors"]] == [f".notes/20_projects/{P}/01_見積/b.pdf.md"]
    assert sorted(result["changed"]) == sorted(
        [f".notes/{REL}.md", f".notes/20_projects/{P}/01_見積/c.pdf.md"]
    )
    assert result["backup"] is not None
    assert 'resource: "/' in first.read_text(encoding="utf-8")
    assert 'resource: "/' in last.read_text(encoding="utf-8")
    assert 'resource: "/' not in locked.read_text(encoding="utf-8")  # 旧形式のまま
    assert not (data_dir / "reconcile.lock").exists()
