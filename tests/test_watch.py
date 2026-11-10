import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from helpers import make_note, put
from vault_kb import bundle, convert, watch, wiki
from vault_kb.lock import acquire, release
from vault_kb.vault import Vault


def note_for(vault, rel):
    """原本 rel（vault の根から）の隣に、実際の指紋を持つノートを置く。"""
    original = put(vault, rel)
    size, mtime_ns = convert.stat_of(original)
    _, project, category, name = original.relative_to(vault.root).parts
    make_note(vault, project, category, name, size=size, mtime_ns=mtime_ns)
    return original


def test_statusはinbox_案件直下_未登録_孤児_更新の件数と合計を返す(vault, data_dir):
    put(vault, "00_inbox/受信.txt")
    put(vault, "20_projects/案件A/直下.txt")
    put(vault, "20_projects/案件A/共通/未登録.txt")
    make_note(vault, "案件A", "共通", "消えた.txt")
    note_for(vault, "20_projects/案件A/共通/変わらない.txt")
    changed = note_for(vault, "20_projects/案件A/共通/更新.txt")
    changed.write_bytes(b"changed content")

    result = watch.status(vault, data_dir)

    assert result == {
        "vault": str(vault.root),
        "inbox": 1,
        "project_root": 1,
        "unregistered": 1,
        "orphan": 1,
        "changed": 1,
        "pending": 5,
        "stale_projects": 0,
        "stale_queries": 0,
        "busy": False,
    }


def test_statusはconflictsを返さない(vault, data_dir):
    assert "conflicts" not in watch.status(vault, data_dir)


def test_statusはロックが生きていればbusy(vault, data_dir):
    acquire(data_dir, datetime.now().astimezone())

    assert watch.status(vault, data_dir)["busy"] is True


def test_statusは古いロックならbusyでない(vault, data_dir):
    (data_dir / "reconcile.lock").write_text("2020-01-01T00:00:00+09:00", encoding="utf-8")

    assert watch.status(vault, data_dir)["busy"] is False


def test_statusはmtime_nsが整数でないノートで原本を登録済みにしない(vault, data_dir):
    original = put(vault, "20_projects/案件A/共通/a.txt")
    make_note(vault, "案件A", "共通", original.name, size=original.stat().st_size, mtime_ns="x")

    result = watch.status(vault, data_dir)

    assert (result["unregistered"], result["changed"]) == (1, 0)  # a.txt だけ


def test_snapshotは原本だけを相対パスで返し_ノートと予約名は含めない(vault):
    put(vault, "00_inbox/受信.txt", b"abc")
    put(vault, "20_projects/案件A/共通/a.PDF", b"12345")
    put(vault, "20_projects/案件A/共通/メモ.md", b"12")
    put(vault, "00_inbox/index.md")
    put(vault, "直下.txt")  # vault 直下は対象外
    put(vault, "10_daily/メモ.md")  # 利用者の領域は走査しない
    make_note(vault, "案件A", "共通", "a.PDF")

    snap = watch.snapshot(vault)

    assert {rel: size for rel, (size, _) in snap.items()} == {
        "00_inbox/受信.txt": 3,
        "20_projects/案件A/共通/a.PDF": 5,
        "20_projects/案件A/共通/メモ.md": 2,
    }


def test_snapshotはnotes_dirの中を含めない(vault):
    put(vault, ".notes/20_projects/案件A/共通/メモ.txt", b"abc")
    put(vault, "20_projects/案件A/共通/a.txt", b"12")

    assert list(watch.snapshot(vault)) == ["20_projects/案件A/共通/a.txt"]


def test_snapshotはbaseがなければ空(tmp_path):
    assert watch.snapshot(Vault(tmp_path / "なし")) == {}


def counting_sleep(actions):
    """n 回目の sleep で actions[n] を実行する（実際には待たない）。"""
    count = 0

    def sleep(_seconds):
        nonlocal count
        count += 1
        if count in actions:
            actions[count]()

    return sleep


def test_watch_eventsは新しいファイルが落ち着いた次のpollでchangedを1回だけ出す(vault, data_dir):
    sleep = counting_sleep({1: lambda: put(vault, "00_inbox/受信.txt")})

    assert list(watch.watch_events(vault, data_dir, sleep=sleep, rounds=4)) == ["changed"]


def test_watch_eventsは別の片づけが進行中の間はchangedを出さず_終われば出す(vault, data_dir):
    acquire(data_dir, datetime.now().astimezone())
    sleep = counting_sleep(
        {1: lambda: put(vault, "00_inbox/受信.txt"), 4: lambda: release(data_dir)}
    )

    events = []
    for event in watch.watch_events(vault, data_dir, sleep=sleep, rounds=6):
        events.append((event, (data_dir / "reconcile.lock").exists()))

    assert events == [("changed", False)]


def test_watch_eventsは変化がなければ何も出さない(vault, data_dir):
    assert list(watch.watch_events(vault, data_dir, sleep=lambda _: None, rounds=3)) == []


def test_watchはサイズが変わり続ける間はchangedを出さない(vault, data_dir):
    path = put(vault, "00_inbox/受信.txt", b"0")

    def append():
        with path.open("ab") as file:
            file.write(b"more")

    actions = {1: append, 2: append, 3: append}

    assert list(watch.watch_events(vault, data_dir, sleep=counting_sleep(actions), rounds=3)) == []
    assert list(watch.watch_events(vault, data_dir, sleep=counting_sleep(actions), rounds=5)) == [
        "changed"
    ]


def test_statusはresourceが名前と違うノートが指す原本を未登録に数えない(vault, data_dir):
    original = put(vault, "20_projects/案件A/共通/b.pdf")
    size, mtime_ns = convert.stat_of(original)
    make_note(
        vault,
        "案件A",
        "共通",
        "a",
        resource="20_projects/案件A/共通/b.pdf",
        size=size,
        mtime_ns=mtime_ns,
    )

    result = watch.status(vault, data_dir)

    assert (result["unregistered"], result["orphan"], result["pending"]) == (0, 0, 0)


@pytest.mark.parametrize("name", ["~$x.docx", ".x.tmp"], ids=["Officeのロック", "一時ファイル"])
def test_watch_eventsは無視する名前のファイルの変化に反応しない(vault, data_dir, name):
    sleep = counting_sleep({1: lambda: put(vault, f"20_projects/案件A/共通/{name}")})

    assert list(watch.watch_events(vault, data_dir, sleep=sleep, rounds=4)) == []


def test_watch_eventsは計画ファイルの書き込みに反応しない(vault, data_dir):
    plan = vault.root / ".vault-kb-plan.json"
    sleep = counting_sleep({1: lambda: plan.write_text('{"items": []}', encoding="utf-8")})

    assert list(watch.watch_events(vault, data_dir, sleep=sleep, rounds=4)) == []


def test_watch_eventsはmdの変化を無視する(vault, data_dir):
    index = vault.root / "index.md"
    sleep = counting_sleep({1: lambda: index.write_text("# 変更", encoding="utf-8")})

    assert list(watch.watch_events(vault, data_dir, sleep=sleep, rounds=4)) == []


def test_request_stopは置き場に停止の印を書く(data_dir):
    watch.request_stop(data_dir, datetime(2026, 11, 2, 9, 0).astimezone())

    assert (data_dir / "watch.stop").is_file()


def test_watch_eventsは停止の印を見るとstoppedを出して終わる(vault, data_dir):
    sleep = counting_sleep({2: lambda: watch.request_stop(data_dir, datetime.now().astimezone())})

    assert list(watch.watch_events(vault, data_dir, sleep=sleep, rounds=10)) == ["stopped"]


def test_watch_eventsは停止の印のあとに起きた変化を出さない(vault, data_dir):
    def stop_and_put():
        watch.request_stop(data_dir, datetime.now().astimezone())
        put(vault, "00_inbox/受信.txt")

    sleep = counting_sleep({1: stop_and_put})

    assert list(watch.watch_events(vault, data_dir, sleep=sleep, rounds=10)) == ["stopped"]


def test_watch_eventsは起動より前の停止の印を無視して動き続け_印を消さない(vault, data_dir):
    watch.request_stop(data_dir, datetime(2020, 1, 1).astimezone())

    assert list(watch.watch_events(vault, data_dir, sleep=lambda _: None, rounds=3)) == []
    assert (data_dir / "watch.stop").is_file()


@pytest.mark.parametrize("content", ["", "でたらめ", "2026-13-45T00:00:00", "2026-11-02T00:00:00"])
def test_watch_eventsは時刻が読めない印を無視する(vault, data_dir, content):
    # 最後の値は時刻の形式が正しいが、タイムゾーンがなく比べられない
    (data_dir / "watch.stop").write_text(content, encoding="utf-8")

    assert list(watch.watch_events(vault, data_dir, sleep=lambda _: None, rounds=3)) == []


def test_unwatchのあとに起動した別のwatchは止まらず_古いwatchだけが止まる(vault, data_dir):
    new_events = []

    def stop_then_start_new_watch():
        watch.request_stop(data_dir, datetime.now().astimezone())
        time.sleep(0.05)  # 新しい watch の開始時刻を、印より後にする
        new_events.extend(watch.watch_events(vault, data_dir, sleep=lambda _: None, rounds=3))

    sleep = counting_sleep({1: stop_then_start_new_watch})

    assert list(watch.watch_events(vault, data_dir, sleep=sleep, rounds=10)) == ["stopped"]
    assert new_events == []


def test_open_itemsは読めない資料を一覧に出すが未解消には数えない(vault, data_dir):
    put(vault, "00_inbox/受信.txt")
    note_for(vault, "20_projects/案件A/共通/ok.pdf")
    unreadable = put(vault, "20_projects/案件A/共通/読めない.pdf")
    size, mtime_ns = convert.stat_of(unreadable)
    make_note(vault, "案件A", "共通", "読めない.pdf", size=size, mtime_ns=mtime_ns, readable=False)
    acquire(data_dir, datetime.now().astimezone())

    result = watch.open_items(vault, data_dir)

    assert result["inbox"] == 1
    assert result["pending"] == 1
    assert result["busy"] is True
    assert result["unreadable"] == [str(unreadable)]
    assert Path(result["unreadable"][0]).is_absolute()
    assert result["unresolved"] == 2  # 片づけ待ち 1 + 進行中のロック 1


def test_open_itemsは何もなければ未解消0(vault, data_dir):
    result = watch.open_items(vault, data_dir)

    assert (result["pending"], result["busy"], result["unreadable"], result["unresolved"]) == (
        0,
        False,
        [],
        0,
    )


def test_snapshotは案件の下のドット始まりのフォルダの中を含めない(vault):
    put(vault, "20_projects/案件A/共通/a.txt")
    put(vault, "20_projects/案件A/共通/.obsidian/workspace.json")
    put(vault, "20_projects/案件A/.git/config")

    assert list(watch.snapshot(vault)) == ["20_projects/案件A/共通/a.txt"]


NOW = datetime(2026, 11, 10, 9, 30, tzinfo=UTC)


def _note_with_original(vault, name, sha):
    """実際のサイズと更新時刻を持つノート（片づけ待ちに数えられない）を置く。"""
    original = put(vault, f"20_projects/A/01_x/{name}")
    size, mtime_ns = convert.stat_of(original)
    make_note(vault, "A", "01_x", name, sha256=sha * 64, size=size, mtime_ns=mtime_ns)


def _wiki_with_page_and_query(vault, data_dir, evidence=("a.xlsx",)):
    """案件 A の統合ページと、回答（根拠は evidence）を 1 件ずつ保存する。"""
    for name, sha in (("a.xlsx", "a"), ("b.xlsx", "b")):
        _note_with_original(vault, name, sha)
    page = {
        "op": "page",
        "kind": "project",
        "name": "A",
        "body": "本文",
        "expected_digest": wiki.digest(wiki.project_entries(vault, "A")),
    }
    query = {
        "op": "query",
        "question": "質問",
        "answer": "回答",
        "evidence": [f"20_projects/A/01_x/{name}" for name in evidence],
        "scope": ["A"],
        "slug": "q",
    }
    wiki.apply(vault, data_dir, {"ops": [page, query]}, NOW)


def test_statusは古い統合ページと再利用できない回答の件数を参考値として返す(vault, data_dir):
    _wiki_with_page_and_query(vault, data_dir)

    fresh = watch.status(vault, data_dir)
    assert (fresh["stale_projects"], fresh["stale_queries"]) == (0, 0)

    _note_with_original(vault, "c.xlsx", "c")  # ページと回答のあとに、同じ案件のノートが増えた
    stale = watch.status(vault, data_dir)

    assert (stale["stale_projects"], stale["stale_queries"]) == (1, 1)


def test_statusの古さの件数はpendingとunresolvedに入れない(vault, data_dir):
    _wiki_with_page_and_query(vault, data_dir)
    before = watch.status(vault, data_dir)["pending"]
    _note_with_original(vault, "c.xlsx", "c")

    status = watch.status(vault, data_dir)
    items = watch.open_items(vault, data_dir)

    assert status["pending"] == before == 0
    assert items["unresolved"] == 0  # 古い回答だけで、見張りの終了確認を出さない


def test_statusは根拠のない回答を再利用できない回答に数えない(vault, data_dir):
    _wiki_with_page_and_query(vault, data_dir, evidence=())  # 「資料からは確認できない」回答

    assert watch.status(vault, data_dir)["stale_queries"] == 0


def test_statusは原本を同じ案件内で動かしただけなら回答を古いとしない(vault, data_dir):
    _wiki_with_page_and_query(vault, data_dir)
    (vault.notes_dir / "20_projects/A/01_x/a.xlsx.md").unlink()
    _note_with_original(vault, "a2.xlsx", "a")  # sha256 は同じで、パスだけ変わった

    status = watch.status(vault, data_dir)

    assert status["stale_queries"] == 0
    assert status["stale_projects"] == 1  # ページの出典のパスは古い（移動の反映は次の更新）


def test_statusは全ノートの走査を1回だけにする(vault, data_dir, monkeypatch):
    _wiki_with_page_and_query(vault, data_dir)
    calls = []
    scan = bundle.notes
    monkeypatch.setattr(bundle, "notes", lambda v: calls.append(1) or scan(v))

    watch.status(vault, data_dir)

    assert len(calls) == 1


def test_statusは根拠が複数で一部だけ動いた回答を古いとしない(vault, data_dir):
    _wiki_with_page_and_query(vault, data_dir, evidence=("a.xlsx", "b.xlsx"))
    (vault.notes_dir / "20_projects/A/01_x/a.xlsx.md").unlink()
    _note_with_original(vault, "a2.xlsx", "a")  # a だけ動いた（b はそのまま）

    assert watch.status(vault, data_dir)["stale_queries"] == 0


def test_statusは範囲が空の回答を別の案件へ動かしても古いとしない(vault, data_dir):
    for name, sha in (("a.xlsx", "a"), ("b.xlsx", "b")):
        _note_with_original(vault, name, sha)
    op = {
        "op": "query",
        "question": "全体",
        "answer": "回答",
        "evidence": ["20_projects/A/01_x/a.xlsx"],
        "scope": [],  # vault 全体
        "slug": "q",
    }
    wiki.apply(vault, data_dir, {"ops": [op]}, NOW)
    (vault.notes_dir / "20_projects/A/01_x/a.xlsx.md").unlink()
    original = put(vault, "20_projects/B/01_x/a.xlsx")
    size, mtime_ns = convert.stat_of(original)
    make_note(vault, "B", "01_x", "a.xlsx", sha256="a" * 64, size=size, mtime_ns=mtime_ns)

    assert watch.status(vault, data_dir)["stale_queries"] == 0


def test_statusは壊れた_wiki_があっても落ちず_読めないページは数えない(vault, data_dir):
    _wiki_with_page_and_query(vault, data_dir)
    queries = vault.wiki_dir / "queries"
    (queries / "壊れ.md").write_bytes(b"\xff\xfe\x00 not utf-8")
    (queries / "con.md").write_text('---\ntype: "query"\n---\n', encoding="utf-8")
    (vault.wiki_dir / "projects" / "B.md").write_text("---\nsources: 1\n---\n", encoding="utf-8")

    status = watch.status(vault, data_dir)

    assert (status["stale_projects"], status["stale_queries"]) == (0, 0)


def test_statusは回答のファイルを読めなくても落ちず_その分を数えない(vault, data_dir, monkeypatch):
    _wiki_with_page_and_query(vault, data_dir)
    _note_with_original(vault, "c.xlsx", "c")
    real = wiki.read_page

    def locked(path):
        if path.parent.name == "queries":
            raise PermissionError("使用中")
        return real(path)

    monkeypatch.setattr(wiki, "read_page", locked)

    status = watch.status(vault, data_dir)

    assert (status["stale_projects"], status["stale_queries"]) == (1, 0)
