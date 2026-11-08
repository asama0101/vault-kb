"""run.py approve（PreToolUse フック）: vault-kb 自身の片づけの操作だけを確認なしで通す。"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from vault_kb.vault import save_config

PLUGIN = Path(__file__).resolve().parent.parent / "plugins" / "vault-kb"
RUN = PLUGIN / "run.py"
RECONCILE = PLUGIN / "references" / "reconcile.md"
ALLOW = {
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "permissionDecisionReason": "vault-kb の片づけ",
    }
}


def approve_raw(data: Path, stdin: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("VAULT_KB_HOME", "PYTHONUTF8")}
    return subprocess.run(
        [sys.executable, str(RUN), "--data", str(data), "approve"],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
    )


def approve(data: Path, tool_name: str, tool_input: dict) -> subprocess.CompletedProcess:
    """フックの入力（stdin の JSON）を渡して approve を実行する。"""
    return approve_raw(data, json.dumps({"tool_name": tool_name, "tool_input": tool_input}))


@pytest.fixture
def env(tmp_path):
    """configure 済みの (置き場, vault の根, run.py コマンドの先頭)。専用環境はない。"""
    data = tmp_path / "置き場 data"
    data.mkdir()
    root = tmp_path / "仕事 vault"
    root.mkdir()
    save_config(data, root)
    head = f'python "{RUN.as_posix()}" --data "{data.as_posix()}"'
    return data, root, head


def plan_of(root: Path) -> Path:
    return root / ".vault-kb-plan.json"


@pytest.mark.parametrize("sub", ["status", "prepare", "watch", "unwatch", "record-stop"])
def test_自分のrun_pyの片づけのコマンドを許可する(env, sub):
    data, _, head = env

    result = approve(data, "Bash", {"command": f"{head} {sub}"})

    assert result.returncode == 0
    assert json.loads(result.stdout) == ALLOW


def test_releaseは承認なしで通さない(env):
    data, _, head = env

    result = approve(data, "Bash", {"command": f"{head} release"})

    assert result.stdout == ""


def test_計画ファイルを渡したapplyを許可する(env):
    data, root, head = env

    result = approve(data, "Bash", {"command": f'{head} apply "{plan_of(root).as_posix()}"'})

    assert json.loads(result.stdout) == ALLOW


def test_バックスラッシュ区切りの同じパスも許可する(env):
    data, root, _ = env
    command = f'python "{RUN}" --data "{data}" apply "{plan_of(root)}"'
    assert "\\" in command

    result = approve(data, "Bash", {"command": command})

    assert json.loads(result.stdout) == ALLOW


@pytest.mark.parametrize(
    "tail",
    [
        "setup --yes",
        "configure --vault C:/x",
        "reindex",
        "approve",
        "doctor",
        "prepare; echo hi",
        "prepare && ls",
        "prepare | cat",
        "status status",
        'apply "C:/x/other/.vault-kb-plan.json"',
        'apply "{plan}" extra',
        "apply",
        "",
    ],
    ids=[
        "setup",
        "configure",
        "reindex",
        "approve",
        "doctor",
        "セミコロン",
        "and",
        "パイプ",
        "引数過多",
        "別の計画",
        "apply 引数過多",
        "apply 引数なし",
        "サブコマンドなし",
    ],
)
def test_片づけ以外のrun_pyコマンドは判定しない(env, tail):
    data, root, head = env
    command = f"{head} {tail.format(plan=plan_of(root).as_posix())}".rstrip()

    result = approve(data, "Bash", {"command": command})

    assert result.returncode == 0
    assert result.stdout == ""


@pytest.mark.parametrize(
    "command",
    [
        'python "C:/other/run.py" --data "{data}" prepare',
        'python "{run}" --data "C:/other" prepare',
        'python -c "print(1)"',
        'python "{run}" prepare',
        'py "{run}" --data "{data}" prepare',
        'cd /x && python "{run}" --data "{data}" prepare',
        "ls",
    ],
    ids=["別のrun_py", "別の置き場", "python -c", "--data なし", "py", "cd 付き", "別コマンド"],
)
def test_自分のrun_pyと置き場でないコマンドは判定しない(env, command):
    data, _, _ = env

    result = approve(
        data, "Bash", {"command": command.format(run=RUN.as_posix(), data=data.as_posix())}
    )

    assert result.stdout == ""


@pytest.mark.parametrize("tool", ["Read", "Write", "Edit"])
def test_計画ファイルの読み書きを許可する(env, tool):
    data, root, _ = env

    result = approve(data, tool, {"file_path": plan_of(root).as_posix()})

    assert json.loads(result.stdout) == ALLOW


def test_手順書reconcile_mdの読み取りを許可する(env):
    data, _, _ = env

    result = approve(data, "Read", {"file_path": RECONCILE.as_posix()})

    assert json.loads(result.stdout) == ALLOW


@pytest.mark.parametrize(
    ("tool", "rel"),
    [
        ("Write", "index.md"),
        ("Edit", ".notes/20_projects/A社/01_見積/見積.xlsx.md"),
        ("Read", "メモ.md"),
        ("Write", "other/.vault-kb-plan.json"),
    ],
    ids=["index", "ノート", "vault のメモ", "別のフォルダの計画"],
)
def test_計画ファイルと手順書以外の読み書きは判定しない(env, tool, rel):
    data, root, _ = env

    result = approve(data, tool, {"file_path": (root / rel).as_posix()})

    assert result.stdout == ""


def test_手順書の書き込みは判定しない(env):
    data, _, _ = env

    result = approve(data, "Write", {"file_path": RECONCILE.as_posix()})

    assert result.stdout == ""


def test_vaultが未設定なら計画ファイルを許可しない(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    plan = tmp_path / "v" / ".vault-kb-plan.json"

    result = approve(data, "Write", {"file_path": plan.as_posix()})

    assert result.returncode == 0
    assert result.stdout == ""


def test_vaultが空文字の設定なら作業フォルダの計画ファイルも許可しない(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"vault": ""}), encoding="utf-8")
    cwd_plan = Path.cwd() / ".vault-kb-plan.json"  # Path("") は現在のフォルダを指してしまう

    result = approve(data, "Write", {"file_path": str(cwd_plan)})

    assert result.returncode == 0
    assert result.stdout == ""


def test_Monitorはwatchのコマンドだけを許可する(env):
    data, _, head = env

    watch = approve(data, "Monitor", {"command": f"{head} watch", "description": "vault-kb の変化"})
    other = approve(data, "Monitor", {"command": f"{head} prepare"})

    assert json.loads(watch.stdout) == ALLOW
    assert other.stdout == ""


@pytest.mark.parametrize(
    "stdin",
    ["", "{", '{"tool_name": "Bash"}', '{"tool_name": "Bash", "tool_input": {}}', "[]"],
    ids=["空", "壊れたJSON", "tool_input なし", "command なし", "リスト"],
)
def test_壊れた入力でも何も出さず終了コード0(env, stdin):
    data, _, _ = env

    result = approve_raw(data, stdin)

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_ジャンクション経由の置き場も同じものとして許可する(env, tmp_path):
    data, _, _ = env
    link = tmp_path / "link"
    cmd = os.environ.get("COMSPEC", "cmd.exe")
    made = subprocess.run(
        [cmd, "/c", "mklink", "/J", str(link), str(data)], capture_output=True, check=False
    )
    if made.returncode != 0:
        pytest.skip("mklink /J が使えない環境（Windows 以外・制限された環境）")
    command = f'python "{RUN.as_posix()}" --data "{link.as_posix()}" status'

    result = approve(data, "Bash", {"command": command})

    assert json.loads(result.stdout) == ALLOW


def test_置き場がないときも何も出さず終了コード0(tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in ("VAULT_KB_HOME", "PYTHONUTF8")}

    result = subprocess.run(
        [sys.executable, str(RUN), "approve"],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == ""


WIKI_GUIDE = PLUGIN / "references" / "wiki.md"


def wiki_plan_of(root: Path) -> Path:
    return root / ".vault-kb-wiki-plan.json"


@pytest.mark.parametrize("sub", ["wiki-check", "wiki-apply"])
def test_wiki_check_wiki_applyは自分の計画だけ通す(env, sub):
    data, root, head = env
    plan = wiki_plan_of(root)

    slash = approve(data, "Bash", {"command": f'{head} {sub} "{plan.as_posix()}"'})
    back = approve(data, "Bash", {"command": f'python "{RUN}" --data "{data}" {sub} "{plan}"'})

    assert json.loads(slash.stdout) == ALLOW
    assert json.loads(back.stdout) == ALLOW


@pytest.mark.parametrize("sub", ["wiki-check", "wiki-apply"])
@pytest.mark.parametrize(
    "tail",
    [
        "",
        '"C:/x/other/.vault-kb-wiki-plan.json"',
        '"{plan}" extra',
        '"{plan}" "{plan}"',
        '"{plan}"; ls',
        '"{plan}" && ls',
        '"{plan}" | cat',
        '"{vault}/.vault-kb-plan.json"',
        "--help",
    ],
    ids=[
        "引数なし",
        "別パス",
        "付け足し",
        "二重",
        "セミコロン",
        "and",
        "パイプ",
        "片づけの計画",
        "オプション",
    ],
)
def test_wiki計画に引数なし_別パス_付け足しは通さない(env, sub, tail):
    data, root, head = env
    tail = tail.format(plan=wiki_plan_of(root).as_posix(), vault=root.as_posix())

    result = approve(data, "Bash", {"command": f"{head} {sub} {tail}".rstrip()})

    assert result.returncode == 0
    assert result.stdout == ""


def test_wiki計画は片づけのapplyの許可に混ざらない(env):
    data, root, head = env

    wiki_to_apply = approve(data, "Bash", {"command": f'{head} apply "{wiki_plan_of(root)}"'})
    plan_to_wiki = approve(data, "Bash", {"command": f'{head} wiki-apply "{plan_of(root)}"'})

    assert wiki_to_apply.stdout == ""
    assert plan_to_wiki.stdout == ""


def test_Monitorはwiki_applyを通さない(env):
    data, root, head = env

    result = approve(data, "Monitor", {"command": f'{head} wiki-apply "{wiki_plan_of(root)}"'})

    assert result.stdout == ""


@pytest.mark.parametrize("tool", ["Read", "Write", "Edit"])
def test_wiki計画のRead_Write_Editを通す(env, tool):
    data, root, _ = env

    result = approve(data, tool, {"file_path": wiki_plan_of(root).as_posix()})

    assert json.loads(result.stdout) == ALLOW


def test_wikiガイドのReadだけを通す(env):
    data, _, _ = env

    read = approve(data, "Read", {"file_path": WIKI_GUIDE.as_posix()})
    write = approve(data, "Write", {"file_path": WIKI_GUIDE.as_posix()})

    assert json.loads(read.stdout) == ALLOW
    assert write.stdout == ""


def test_別のフォルダのwiki計画は通さない(env):
    data, root, _ = env

    result = approve(
        data, "Write", {"file_path": (root / "other" / ".vault-kb-wiki-plan.json").as_posix()}
    )

    assert result.stdout == ""


@pytest.mark.parametrize(
    "stdin",
    ["", "{", '{"tool_name": "Bash", "tool_input": {"command": "python run.py wiki-check"}}'],
    ids=["空", "壊れたJSON", "引数のないコマンド"],
)
def test_wiki版_壊れた入力でも何も出さず終了コード0(env, stdin):
    data, _, _ = env

    result = approve_raw(data, stdin)

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_wiki計画名はrun_pyとcliで同じ():
    from vault_kb import cli  # noqa: PLC0415

    sys.path.insert(0, str(PLUGIN))
    try:
        import run  # noqa: PLC0415
    finally:
        sys.path.remove(str(PLUGIN))

    assert run.WIKI_PLAN_NAME == cli.WIKI_PLAN_NAME


@pytest.mark.parametrize("tool", ["Bash", "Monitor"])
@pytest.mark.parametrize(
    "template",
    [
        '{head} wiki-check "{root}/$(echo aGk= | base64 -d | sh)/../.vault-kb-wiki-plan.json"',
        '{head} wiki-apply "{root}/`id`/../.vault-kb-wiki-plan.json"',
        '{head} apply "{root}/$(id)/../.vault-kb-plan.json"',
        '{head} apply "{root}/`id`/../.vault-kb-plan.json"',
        '{head} wiki-check "{root}/.vault-kb-wiki-plan.json$(id)"',
        '{head} wiki-apply "{root}/../{name}/.vault-kb-wiki-plan.json"',
        '{head} apply "{root}/../{name}/.vault-kb-plan.json"',
        'python "{run}/$(id)/../run.py" --data "{data}" wiki-check "{wiki}"',
        'python "{run}/`id`/../run.py" --data "{data}" apply "{plan}"',
        'python "{run}" --data "{data}/$(id)/../" wiki-apply "{wiki}"',
        'python "{run}" --data "{data}/`id`/../{dname}" apply "{plan}"',
        'python "{run}" --data "{data}/../{dname}" wiki-check "{wiki}"',
        'python "{run}/../run.py" --data "{data}" apply "{plan}"',
        '{head} wiki-check "{wiki}\n"',
        "{head} status $(id)",
        "{head} watch `id`",
    ],
)
def test_シェル展開と親ディレクトリ参照を含むコマンドは通さない(env, tool, template):
    data, root, head = env
    command = template.format(
        head=head,
        root=root.as_posix(),
        name=root.name,
        run=RUN.as_posix(),
        data=data.as_posix(),
        dname=data.name,
        plan=plan_of(root).as_posix(),
        wiki=wiki_plan_of(root).as_posix(),
    )

    result = approve(data, tool, {"command": command})

    assert result.returncode == 0
    assert result.stdout == ""
