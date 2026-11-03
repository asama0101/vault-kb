"""同名・別内容の資料の保留（.vault-kb-conflicts.json）と、その解決。"""

import json
import shutil
import subprocess
from datetime import datetime, timezone

import pytest
from helpers import make_note, original_rel
from sample_docs import make_txt
from vault_kb import conflict, convert, watch
from vault_kb.reconcile import apply, prepare
from vault_kb.vault import is_link

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
EXISTING = "先にある原本の中身です。二十字を超える長さにしてある。"
NEW = "後から来た資料の中身です。二十字を超える長さにしてある。"
SOURCE = "00_inbox/x.txt"
DESTINATION = "20_projects/A社/01_見積/x.txt"


def junction(link, target):
    """link に target へのジャンクションを作る（作れなければスキップ）。"""
    command = ["cmd", "/c", "mklink", "/J", str(link), str(target)]
    if subprocess.run(command, capture_output=True, check=False).returncode != 0:  # noqa: S607
        pytest.skip("ジャンクションを作れない環境")


def plan_item(source=SOURCE):
    return {
        "source": source,
        "kind": "inbox",
        "project": "A社",
        "category": "見積",
        "title": "見積書",
        "description": "説明",
        "tags": [],
        "client": "A社",
        "doc_type": "見積書",
        "date": None,
    }


@pytest.fixture
def scene(vault, data_dir):
    """先に原本（ノート付き）があり、00_inbox に同名・別内容の資料がある vault。"""
    existing = vault.path(original_rel("A社", "01_見積", "x.txt"))
    existing.parent.mkdir(parents=True)
    make_txt(existing, EXISTING)
    size, mtime_ns = convert.stat_of(existing)
    make_note(
        vault,
        "A社",
        "01_見積",
        "x.txt",
        sha256=convert.sha256_of(existing),
        size=size,
        mtime_ns=mtime_ns,
    )
    incoming = vault.path(SOURCE)
    make_txt(incoming, NEW)
    return existing, incoming


def hold(vault, data_dir):
    """prepare → apply で、00_inbox/x.txt を保留にする。"""
    prepare(vault, data_dir, NOW)
    return apply(vault, data_dir, {"items": [plan_item()]}, NOW)


def test_applyは同名で中身が違う資料を動かさずconflictにして保留の記録を書く(
    vault, data_dir, scene
):
    existing, incoming = scene

    result = hold(vault, data_dir)

    assert result["results"] == [
        {
            "source": SOURCE,
            "status": "conflict",
            "note": None,
            "message": None,
            "original": str(existing),
        }
    ]
    assert result["done"] == []
    assert incoming.read_text(encoding="utf-8") == NEW
    assert existing.read_text(encoding="utf-8") == EXISTING
    (record,) = json.loads((vault.base / ".vault-kb-conflicts.json").read_text(encoding="utf-8"))
    assert (record["source"], record["destination"]) == (SOURCE, DESTINATION)
    assert set(record) == {"source", "destination", "size", "mtime_ns"}
    assert record["size"] == incoming.stat().st_size


def test_保留中の資料はprepareの項目とstatusのpendingに出ずconflictsに数える(
    vault, data_dir, scene
):
    hold(vault, data_dir)

    result = prepare(vault, data_dir, NOW)
    status = watch.status(vault, data_dir)

    assert result["items"] == []
    assert (status["inbox"], status["pending"], status["conflicts"]) == (0, 0, 1)


def test_保留した資料が変更されると保留は無効になり普通に扱い直される(vault, data_dir, scene):
    _, incoming = scene
    hold(vault, data_dir)

    make_txt(incoming, NEW + "追記して、サイズを変える。")

    assert watch.status(vault, data_dir)["conflicts"] == 0
    assert [i["source"] for i in prepare(vault, data_dir, NOW)["items"]] == [SOURCE]


def test_保留した資料が消えると保留は無効になる(vault, data_dir, scene):
    _, incoming = scene
    hold(vault, data_dir)

    incoming.unlink()

    assert watch.status(vault, data_dir)["conflicts"] == 0


def test_同じ資料がもう一度衝突しても保留は1件のまま(vault, data_dir, scene):
    hold(vault, data_dir)
    prepare(vault, data_dir, NOW)
    # 保留の記録を消して、もう一度 apply しても、記録は 1 件
    (vault.base / ".vault-kb-conflicts.json").unlink()
    hold(vault, data_dir)
    hold(vault, data_dir)

    assert len(conflict.holds(vault)) == 1


def test_overwriteは先の原本を保留の資料で置き換えlogの行を返す(vault, data_dir, scene):
    existing, incoming = scene
    hold(vault, data_dir)

    line = conflict.resolve(vault, SOURCE, "overwrite")

    assert existing.read_text(encoding="utf-8") == NEW
    assert not incoming.exists()
    assert conflict.holds(vault) == []
    assert line == f"**Overwrote** x.txt — 元: {incoming} / 先: {existing}"


def test_overwriteのあと次の片づけで先のノートが作り直される(vault, data_dir, scene):
    existing, _ = scene
    hold(vault, data_dir)
    conflict.resolve(vault, SOURCE, "overwrite")

    items = prepare(vault, data_dir, NOW)["items"]

    assert [(i["source"], i["kind"]) for i in items] == [(DESTINATION, "changed")]
    assert existing.read_text(encoding="utf-8") == NEW


def test_discardは保留の資料を消し先の原本は変えずlogの行を返す(vault, data_dir, scene):
    existing, incoming = scene
    hold(vault, data_dir)

    line = conflict.resolve(vault, SOURCE, "discard")

    assert not incoming.exists()
    assert existing.read_text(encoding="utf-8") == EXISTING
    assert conflict.holds(vault) == []
    assert line == f"**Discarded** x.txt（後続を削除） — 元: {incoming} / 既存: {existing}"


def test_resolveは保留でない資料をValueErrorにする(vault, scene):
    with pytest.raises(ValueError, match="同名の確認待ちではありません"):
        conflict.resolve(vault, SOURCE, "overwrite")


def test_resolveは先がリンクならValueErrorにして何も動かさない(vault, data_dir, scene, tmp_path):
    existing, incoming = scene
    hold(vault, data_dir)
    outside = tmp_path / "外"
    outside.mkdir()
    # 先のフォルダ 01_見積 を、vault の外へのリンクに差し替える
    folder = existing.parent
    for child in folder.iterdir():
        child.unlink()
    folder.rmdir()
    junction(folder, outside)
    assert is_link(folder)

    with pytest.raises(ValueError, match="同名の確認待ちではありません"):
        conflict.resolve(vault, SOURCE, "overwrite")

    assert incoming.read_text(encoding="utf-8") == NEW
    assert list(outside.iterdir()) == []


def _record(**override):
    base = {
        "source": "00_inbox/x.txt",
        "destination": "a/b.txt",
        "size": 1,
        "mtime_ns": 1,
    }
    return json.dumps([base | override])


@pytest.mark.parametrize(
    "content",
    [
        "壊れた",
        "{}",
        _record(source="../外.txt"),
        _record(source="/abs.txt"),
        _record(size="1"),
        _record(destination="20_projects/A社/01_見積/a" + chr(0) + "b.txt"),
    ],
    ids=["JSONでない", "配列でない", "親へ出る", "絶対パス", "型が違う", "NUL文字"],
)
def test_holdsは壊れた保留の記録を無視する(vault, scene, content):
    (vault.base / ".vault-kb-conflicts.json").write_text(content, encoding="utf-8")

    assert conflict.holds(vault) == []


def _tamper(vault, victim, **override):
    """victim の実際の stat を持つ、改ざんされた保留の記録を書く。"""
    size, mtime_ns = convert.stat_of(victim)
    record = {
        "source": override.pop("source", None) or vault.rel(victim),
        "destination": DESTINATION,
        "size": size,
        "mtime_ns": mtime_ns,
    } | override
    (vault.base / ".vault-kb-conflicts.json").write_text(json.dumps([record]), encoding="utf-8")
    return record["source"]


@pytest.mark.parametrize("action", ["discard", "overwrite"])
@pytest.mark.parametrize(
    "victim_rel",
    [
        "20_projects/A社/01_見積/y.txt",  # 区分の下の原本（00_inbox 直下でも案件直下でもない）
        "log.md",
        ".notes/20_projects/A社/01_見積/x.txt.md",
    ],
    ids=["区分の下の原本", "log.md", "ノート"],
)
def test_改ざんされた記録のsourceでは_vault内の任意のファイルを消さず置き換えない(
    vault, scene, victim_rel, action
):
    victim = vault.path(victim_rel)
    victim.parent.mkdir(parents=True, exist_ok=True)
    victim.write_text("守るべき中身です。二十字を超える長さにしてある。", encoding="utf-8")
    source = _tamper(vault, victim)

    assert conflict.holds(vault) == []
    with pytest.raises(ValueError, match="同名の確認待ちではありません"):
        conflict.resolve(vault, source, action)
    assert victim.read_text(encoding="utf-8").startswith("守るべき中身")


@pytest.mark.parametrize("destination", [".obsidian/x.txt", "20_projects/A社/x.txt", "log.md"])
def test_改ざんされた記録のdestinationが区分の下の原本でなければ無効(vault, scene, destination):
    _, incoming = scene
    _tamper(vault, incoming, destination=destination)

    assert conflict.holds(vault) == []


def test_sourceとdestinationが同じ記録は無効でdiscardしても原本を消さない(vault, scene):
    existing, _ = scene
    # 区分の下の原本は source として無効。00_inbox 直下に同じパスを指す記録も作れない
    _tamper(vault, existing, destination=vault.rel(existing))

    assert conflict.holds(vault) == []
    assert existing.exists()


def test_sourceが00_inboxのリンクの先なら無効で外のファイルを消さない(vault, scene, tmp_path):
    outside = tmp_path / "外"
    outside.mkdir()
    victim = outside / "f.txt"
    victim.write_text("外のファイル。二十字を超える長さにしてある。", encoding="utf-8")
    junction(vault.base / "00_inbox" / "j", outside)
    source = _tamper(vault, victim, source="00_inbox/j/f.txt")

    assert conflict.holds(vault) == []
    with pytest.raises(ValueError, match="同名の確認待ちではありません"):
        conflict.resolve(vault, source, "discard")
    assert victim.exists()


@pytest.mark.parametrize(
    "override",
    [
        {"destination": "20_projects/A社/01_見積/a:b.txt"},
        {"destination": "20_projects/A社/01_見積/a*b.txt"},
        {"destination": "20_projects/A社/01_見積/x.txt."},
        {"destination": "20_projects/A社/01_見積/x.txt "},
        {"destination": "20_projects/A社/01_見積/" + "a" * 5000},
        {"destination": "20_projects/A社/01_見積/NUL"},
        {"destination": "20_projects/A社/01_見積/con.txt"},
        {"destination": "20_projects/A社/01_見積/COM1"},
        {"destination": "20_projects/A社/01_見積/lpt9.log"},
        {"source": "00_inbox/x?.txt"},
        {"source": "00_inbox/" + "a" * 5000},
        {"source": "00_inbox/a" + chr(1) + "b.txt"},
    ],
    ids=[
        "コロン",
        "アスタリスク",
        "末尾ドット",
        "末尾空白",
        "長い先",
        "NUL",
        "CON拡張子付き",
        "COM1",
        "LPT9拡張子付き",
        "疑問符",
        "長い元",
        "制御文字",
    ],
)
def test_Windowsで使えない名前の記録は例外を出さず無効になる(vault, scene, override):
    _, incoming = scene
    _tamper(vault, incoming, **override)

    assert conflict.holds(vault) == []
    assert conflict.listing(vault) == []


def test_00_inbox自体がリンクなら無効で外のファイルを消さない(vault, scene, tmp_path):
    outside = tmp_path / "外"
    outside.mkdir()
    victim = outside / "x.txt"
    victim.write_text("外のファイル。二十字を超える長さにしてある。", encoding="utf-8")
    shutil.rmtree(vault.base / "00_inbox")
    junction(vault.base / "00_inbox", outside)
    source = _tamper(vault, victim, source=SOURCE)

    assert conflict.holds(vault) == []
    with pytest.raises(ValueError, match="同名の確認待ちではありません"):
        conflict.resolve(vault, source, "discard")
    assert victim.exists()


def test_20_projects自体がリンクなら無効で外のファイルを消さない(vault, scene, tmp_path):
    outside = tmp_path / "外"
    (outside / "A" / "01_x").mkdir(parents=True)
    victim = outside / "A" / "a.txt"
    victim.write_text("外のファイル。二十字を超える長さにしてある。", encoding="utf-8")
    shutil.rmtree(vault.base / "20_projects")
    junction(vault.base / "20_projects", outside)
    source = _tamper(
        vault, victim, source="20_projects/A/a.txt", destination="20_projects/A/01_x/a.txt"
    )

    assert conflict.holds(vault) == []
    with pytest.raises(ValueError, match="同名の確認待ちではありません"):
        conflict.resolve(vault, source, "discard")
    assert victim.exists()


def test_destinationの祖先がリンクなら無効で外のファイルを置き換えない(
    vault, data_dir, scene, tmp_path
):
    _, incoming = scene
    outside = tmp_path / "外"
    (outside / "01_見積").mkdir(parents=True)
    target = outside / "01_見積" / "x.txt"
    target.write_text("外の原本。二十字を超える長さにしてある。", encoding="utf-8")
    hold(vault, data_dir)  # 保留の記録は本物で作る
    project = vault.base / "20_projects" / "A社"
    shutil.rmtree(project)  # A社 を、外へのリンクに差し替える
    junction(project, outside)

    assert conflict.holds(vault) == []
    with pytest.raises(ValueError, match="同名の確認待ちではありません"):
        conflict.resolve(vault, SOURCE, "overwrite")
    assert target.read_text(encoding="utf-8").startswith("外の原本")
    assert incoming.exists()


def test_案件直下の資料の保留もprepareの項目に出ない(vault, data_dir, scene):
    root_file = vault.path("20_projects/A社/x.txt")
    make_txt(root_file, NEW)
    prepare(vault, data_dir, NOW)
    result = apply(vault, data_dir, {"items": [plan_item("20_projects/A社/x.txt")]}, NOW)

    assert [r["status"] for r in result["results"]] == ["conflict"]
    sources = [i["source"] for i in prepare(vault, data_dir, NOW)["items"]]
    assert sources == [SOURCE]  # 00_inbox 側は保留していないので、残る
    assert root_file.exists()


def test_listingは元と先のフルパス_サイズ_更新日時を返す(vault, data_dir, scene):
    existing, incoming = scene
    hold(vault, data_dir)

    (item,) = conflict.listing(vault)

    assert item["source"] == SOURCE
    assert item["source_path"] == str(incoming)
    assert item["source_size"] == incoming.stat().st_size
    assert item["destination_path"] == str(existing)
    assert item["destination_size"] == existing.stat().st_size
    assert datetime.fromisoformat(item["source_mtime"]).tzinfo is not None
    assert datetime.fromisoformat(item["destination_mtime"]).tzinfo is not None


def test_notify_linesは保留があれば案内の行を出す():
    lines = watch.notify_lines({"pending": 0, "conflicts": 2})

    assert lines == ["同名の確認待ちが 2 件あります（/vault-kb:resolve で選べます）"]


def test_open_itemsは保留を未解消に数える(vault, data_dir, scene):
    hold(vault, data_dir)

    result = watch.open_items(vault, data_dir)

    assert (result["pending"], result["conflicts"], result["unresolved"]) == (0, 1, 1)
