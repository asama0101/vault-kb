import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from okf_check import check_bundle
from sample_docs import make_txt
from vault_kb.bundle import is_note, read_frontmatter
from vault_kb.cli import main

KB = Path(__file__).resolve().parent.parent / "plugins" / "vault-kb" / "kb.py"
TEMPLATES = [
    "Claude指示プロンプト.md",
    "デイリーノート.md",
    "ノウハウ.md",
    "プロジェクト概要.md",
    "メモ.md",
    "メール下書き.md",
    "議事録.md",
]
TEXT = "第3四半期の販売計画と予算の見直しについて、各部門の数値を確認する"


def _env(data: Path) -> dict[str, str]:
    return {**os.environ, "VAULT_KB_HOME": str(data), "PYTHONUTF8": "1"}


def kb(data: Path, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(KB), *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_env(data),
        check=False,
    )


@pytest.fixture
def configured(tmp_path):
    """configure 済みの (vault のパス, 置き場)。"""
    data = tmp_path / "data dir"
    root = tmp_path / "仕事 vault"
    root.mkdir()
    result = kb(data, "configure", "--vault", str(root))
    assert result.returncode == 0, result.stderr
    return root, data


def test_configureはvault直下に土台のフォルダとindex_mdとlog_mdを作る(configured):
    root, _ = configured
    base = root

    for name in ("00_inbox", "20_projects", "80_archive", "10_daily", "90_attachments"):
        assert (base / name).is_dir(), name
    assert (base / "30_shared" / "99_その他").is_dir()
    assert not (base / "vault-kb").exists()
    assert not (base / "共通").exists()
    assert 'okf_version: "' in (base / "index.md").read_text(encoding="utf-8")
    assert (base / "log.md").read_text(encoding="utf-8") == ""
    assert check_bundle(base) == []


def test_configureはtemplatesに7種類のテンプレートとbasesにBaseを置く(configured):
    root, _ = configured

    assert sorted(p.name for p in (root / "91_templates").iterdir()) == sorted(TEMPLATES)
    assert [p.name for p in (root / "92_bases").iterdir()] == ["その日のノート.base"]
    for name in TEMPLATES:
        text = (root / "91_templates" / name).read_text(encoding="utf-8")
        assert text.startswith(f"---\ncreated: {{{{date:YYYY-MM-DD}}}}\ntype: {name[:-3]}\n---\n")
    base = (root / "92_bases" / "その日のノート.base").read_text(encoding="utf-8")
    assert 'file.ctime.format("YYYY-MM-DD") == this.file.name' in base


def test_configureは同名の既存ファイルを上書きせず他のテンプレートは置く(tmp_path):
    root = tmp_path / "v"
    (root / "91_templates").mkdir(parents=True)
    (root / "91_templates" / "議事録.md").write_text("自分の議事録", encoding="utf-8")
    (root / "92_bases").mkdir()
    (root / "92_bases" / "その日のノート.base").write_text("自分の base", encoding="utf-8")

    result = kb(tmp_path / "data", "configure", "--vault", str(root))

    assert result.returncode == 0, result.stderr
    assert (root / "91_templates" / "議事録.md").read_text(encoding="utf-8") == "自分の議事録"
    assert (root / "92_bases" / "その日のノート.base").read_text(encoding="utf-8") == "自分の base"
    assert (root / "91_templates" / "ノウハウ.md").is_file()


def test_テンプレートを区分に置いてもノートと見なされない(configured):
    root, _ = configured
    folder = root / "20_projects" / "案件A" / "01_見積"
    folder.mkdir(parents=True)
    for name in TEMPLATES:
        target = folder / name
        target.write_bytes((root / "91_templates" / name).read_bytes())
        assert not is_note(read_frontmatter(target)), name
    assert len(list(folder.iterdir())) == len(TEMPLATES) == 7


def test_configureは既存のlog_mdを消さない(tmp_path):
    data = tmp_path / "data"
    root = tmp_path / "v"
    root.mkdir()
    (root / "log.md").write_text("## 2026-01-01\n\n- 既存\n", encoding="utf-8")

    result = kb(data, "configure", "--vault", str(root))

    assert result.returncode == 0, result.stderr
    assert "- 既存" in (root / "log.md").read_text(encoding="utf-8")


def test_configureはvaultがないとき終了コード2(tmp_path):
    result = kb(tmp_path / "data", "configure", "--vault", str(tmp_path / "無い"))

    assert result.returncode == 2
    assert result.stderr.startswith("エラー: ")


def test_prepareとapplyの往復でinboxの資料が片づき_statusのpendingが0になる(configured):
    root, data = configured
    make_txt(root / "00_inbox" / "memo.txt", TEXT)
    assert json.loads(kb(data, "status").stdout)["pending"] == 1

    prepared = json.loads(kb(data, "prepare").stdout)
    (item,) = prepared["items"]
    plan = {
        "items": [
            {
                "source": item["source"],
                "kind": "inbox",
                "project": "A社",
                "category": "計画",
                "title": "販売計画",
                "description": "第3四半期の計画",
                "tags": ["計画"],
                "client": "A社",
                "doc_type": "計画書",
                "date": "2026-10-15",
            }
        ]
    }
    plan_path = Path(prepared["plan_path"])  # 手順書どおり vault の中に書く（走査は無視する）
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    applied = kb(data, "apply", str(plan_path))
    status = json.loads(kb(data, "status").stdout)

    assert applied.returncode == 0, applied.stderr
    assert json.loads(applied.stdout)["results"][0]["status"] == "filed"
    assert status["pending"] == 0
    assert status["busy"] is False
    assert check_bundle(root) == []
    assert any((root / "20_projects" / "A社").rglob("memo.txt"))


def test_prepareは資料がなければ空の項目を返してロックを残さない(configured):
    _, data = configured

    result = kb(data, "prepare")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["items"] == []
    assert not (data / "reconcile.lock").exists()


def test_prepareはロック中にbusyを返して終了コード0(configured):
    root, data = configured
    make_txt(root / "00_inbox" / "memo.txt", TEXT)
    assert kb(data, "prepare").returncode == 0  # 項目があるのでロックが残る

    second = kb(data, "prepare")

    assert second.returncode == 0
    assert json.loads(second.stdout) == {"busy": True}


def test_releaseはロックを解く(configured):
    root, data = configured
    make_txt(root / "00_inbox" / "memo.txt", TEXT)
    kb(data, "prepare")
    assert (data / "reconcile.lock").exists()

    result = kb(data, "release")

    assert json.loads(result.stdout) == {"released": True}
    assert not (data / "reconcile.lock").exists()


def test_releaseは計画の持ち主の印が違えば_他の実行が引き継いだロックを解かない(configured):
    root, data = configured
    data.joinpath("reconcile.lock").write_text("2026-11-08T12:16:00+00:00", encoding="utf-8")  # B
    plan = root / ".vault-kb-plan.json"
    plan.write_text(json.dumps({"lock_token": "2026-11-08T12:00:00+00:00"}), encoding="utf-8")  # A

    result = kb(data, "release")

    assert json.loads(result.stdout) == {"released": True}
    assert (data / "reconcile.lock").exists()


def test_releaseは計画の持ち主の印が同じなら解く(configured):
    root, data = configured
    data.joinpath("reconcile.lock").write_text("2026-11-08T12:00:00+00:00", encoding="utf-8")
    plan = root / ".vault-kb-plan.json"
    plan.write_text(json.dumps({"lock_token": "2026-11-08T12:00:00+00:00"}), encoding="utf-8")

    kb(data, "release")

    assert not (data / "reconcile.lock").exists()


def test_reindexは索引を作り直す(configured):
    root, data = configured
    (root / "index.md").unlink()

    result = kb(data, "reindex")

    assert json.loads(result.stdout) == {"reindexed": True}
    assert "okf_version" in (root / "index.md").read_text(encoding="utf-8")


def test_applyは計画の形が不正なら終了コード2(configured, tmp_path):
    _, data = configured
    plan_path = tmp_path / "plan.json"
    plan_path.write_text('{"items": "x"}', encoding="utf-8")

    result = kb(data, "apply", str(plan_path))

    assert result.returncode == 2
    assert "items" in result.stderr


def test_notifyは未設定ならinitを案内し終了コード0(tmp_path):
    result = kb(tmp_path / "data", "notify")

    assert result.returncode == 0
    assert result.stdout == "[vault-kb] vault が未設定です。/vault-kb:init で設定してください。\n"


def test_notifyはvaultが見えないとき案内して終了コード0(configured):
    root, data = configured
    root.rename(root.parent / "移動した")

    result = kb(data, "notify")

    assert result.returncode == 0
    assert result.stdout.startswith("[vault-kb] vault のフォルダが見つかりません")


def test_notifyは片づけ待ちを1行で案内し片づけはしない(configured):
    root, data = configured
    memo = root / "00_inbox" / "memo.txt"
    make_txt(memo, TEXT)

    result = kb(data, "notify")

    assert result.returncode == 0
    assert result.stdout == (
        "[vault-kb] 片づけ待ちが 1 件あります"
        "（/vault-kb:sweep・/vault-kb:watch・/vault-kb:ask で片づきます）\n"
    )
    assert memo.read_text(encoding="utf-8") == TEXT
    assert not (data / "reconcile.lock").exists()


def test_notifyは待ちがなければ何も出さない(configured):
    _, data = configured

    result = kb(data, "notify")

    assert result.returncode == 0
    assert result.stdout == ""
    assert sorted(p.name for p in data.iterdir()) == ["config.json"]


def test_watchは新着を1行のchangedで出す(configured):
    root, data = configured
    proc = subprocess.Popen(
        [sys.executable, str(KB), "watch"],
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=_env(data),
    )
    lines: queue.Queue[str] = queue.Queue()
    threading.Thread(target=lambda: lines.put(proc.stdout.readline()), daemon=True).start()
    target = root / "00_inbox" / "new.txt"
    try:
        # 起動の速さに依らないよう、行が来るまで変化を作り直す（落ち着くのに 2 回の巡回が要る）
        line = ""
        for _ in range(3):
            with target.open("a", encoding="utf-8") as file:
                file.write(TEXT)
            try:
                line = lines.get(timeout=5)
            except queue.Empty:
                continue
            break
    finally:
        proc.kill()
        proc.wait()

    assert line == "changed\n"


def test_notifyはVAULT_KB_HOMEが未設定でも終了コード0(monkeypatch):
    monkeypatch.delenv("VAULT_KB_HOME", raising=False)

    assert main(["notify"]) == 0


def test_notifyは想定外の失敗でも終了コード0でstderrに1行出す(configured):
    _, data = configured
    (data / "config.json").write_bytes(b"\xff")  # UTF-8 として読めない設定

    result = kb(data, "notify")

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == (
        "[vault-kb] 通知を省略しました: UnicodeDecodeError: 'utf-8' codec can't decode "
        "byte 0xff in position 0: invalid start byte\n"
    )


def test_applyの後処理のOSErrorは終了コード1でlog_mdを案内する(configured, tmp_path):
    root, data = configured
    make_txt(root / "00_inbox" / "memo.txt", TEXT)
    (item,) = json.loads(kb(data, "prepare").stdout)["items"]
    blocker = root / ".notes" / "20_projects" / "A社" / "01_計画" / "index.md"
    blocker.mkdir(parents=True)  # 索引を書けない（中身があるので、片づけが消さない）
    (blocker / "x").write_text("x", encoding="utf-8")
    plan = {
        "items": [
            {
                "source": item["source"],
                "kind": "inbox",
                "project": "A社",
                "category": "計画",
                "title": "販売計画",
                "description": "第3四半期の計画",
                "tags": [],
                "client": "",
                "doc_type": "計画書",
                "date": None,
            }
        ]
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")

    result = kb(data, "apply", str(plan_path))

    assert result.returncode == 1
    assert result.stderr.startswith("実行時のエラー: ")
    assert result.stderr.endswith("処理した資料の記録は log.md にあります\n")
    assert "**Filed** memo.txt" in (root / "log.md").read_text(encoding="utf-8")


def test_applyの計画ファイルが無いときはlog_mdを案内せず終了コード1(
    configured, tmp_path, monkeypatch, capsys
):
    _, data = configured
    monkeypatch.setenv("VAULT_KB_HOME", str(data))

    code = main(["apply", str(tmp_path / "無い.json")])

    err = capsys.readouterr().err
    assert code == 1
    assert "log.md" not in err


def test_unwatchは停止の印を書き確認事項をJSONで返す(configured):
    root, data = configured
    make_txt(root / "00_inbox" / "memo.txt", TEXT)

    result = kb(data, "unwatch")

    assert result.returncode == 0
    report = json.loads(result.stdout)
    assert (report["stop_requested"], report["inbox"], report["pending"]) == (True, 1, 1)
    assert (report["busy"], report["unreadable"], report["unresolved"]) == (False, [], 1)
    assert (data / "watch.stop").is_file()


def test_unwatchを続けて呼んでも同じ形で返る(configured):
    _, data = configured

    first, second = kb(data, "unwatch"), kb(data, "unwatch")

    assert (first.returncode, second.returncode) == (0, 0)
    assert json.loads(first.stdout) == json.loads(second.stdout)


def test_record_stopは未解消がなければStoppedだけをlogに足す(configured):
    root, data = configured

    result = kb(data, "record-stop")

    assert result.returncode == 0
    assert json.loads(result.stdout) == {"recorded": True, "unresolved": 0}
    lines = [x for x in (root / "log.md").read_text(encoding="utf-8").splitlines() if x]
    assert lines[1:] == ["- **Stopped** 見張りを止めた"]


def test_record_stopは未解消の件数をlogに足す(configured):
    root, data = configured
    make_txt(root / "00_inbox" / "memo.txt", TEXT)

    result = kb(data, "record-stop")

    assert json.loads(result.stdout) == {"recorded": True, "unresolved": 1}
    log = (root / "log.md").read_text(encoding="utf-8")
    assert "- **Stopped** 見張りを止めた（未解消 1 件）\n" in log


def test_unwatchが止めた見張りのプロセスはstoppedを出して終了コード0で終わる(configured):
    _, data = configured
    proc = subprocess.Popen(
        [sys.executable, str(KB), "watch"],
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=_env(data),
    )
    try:
        line = ""
        for _ in range(5):  # 起動の前に書いた印は消されるので、終わるまで書き直す
            kb(data, "unwatch")
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                continue
            line = proc.stdout.readline()
            break
    finally:
        proc.kill()
        proc.wait()

    assert (line, proc.returncode) == ("stopped\n", 0)


def test_conflictsとresolveはサブコマンドにない():
    from vault_kb import cli

    for argv in (["conflicts"], ["resolve", "x", "overwrite"]):
        with pytest.raises(SystemExit):
            cli._parser().parse_args(argv)
