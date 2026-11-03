"""プラグインの配布物の検査（plugin-rules F1）。"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from vault_kb import __version__

REPO = Path(__file__).resolve().parent.parent
PLUGIN = REPO / "plugins" / "vault-kb"
SKILLS = sorted((PLUGIN / "skills").glob("*/SKILL.md"))
REFERENCES = PLUGIN / "references" / "reconcile.md"
TEXT_FILES = [*SKILLS, REFERENCES, PLUGIN / "hooks" / "hooks.json"]


def frontmatter(path: Path) -> dict[str, str]:
    head = path.read_text(encoding="utf-8").split("---\n")[1]
    return dict(line.split(": ", 1) for line in head.splitlines() if ": " in line)


def test_スキルは5つあり_nameがディレクトリ名と一致する():
    assert [p.parent.name for p in SKILLS] == ["ask", "init", "resolve", "unwatch", "watch"]
    for skill in SKILLS:
        meta = frontmatter(skill)
        assert meta["name"] == skill.parent.name
        assert meta["description"]


def test_init_resolve_unwatch_watchだけが自動起動を禁止している():
    for name in ("init", "resolve", "unwatch", "watch"):
        assert frontmatter(PLUGIN / f"skills/{name}/SKILL.md")["disable-model-invocation"] == "true"
    assert "disable-model-invocation" not in frontmatter(PLUGIN / "skills/ask/SKILL.md")


def allowed_commands(skill: str) -> list[str]:
    """allowed-tools の run.py コマンド（サブコマンドと引数のパターン）。"""
    tools = frontmatter(PLUGIN / f"skills/{skill}/SKILL.md")["allowed-tools"]
    return re.findall(r'run\.py" --data "\$\{CLAUDE_PLUGIN_DATA\}" ([^)]+)\)', tools)


def test_askは状態確認と片づけとロックの解除だけを承認なしで許可する():
    assert allowed_commands("ask") == ["status", "prepare", "apply *", "release"]


def test_watchは片づけのコマンドとMonitorだけを許可しresolveを承認なしで通さない():
    tools = frontmatter(PLUGIN / "skills/watch/SKILL.md")["allowed-tools"].split()

    # resolve は上書き・削除をするので、資料の本文に誘導されても、承認なしで実行されないように
    assert allowed_commands("watch") == ["prepare", "apply *", "release"]
    assert "Monitor" in tools


def test_resolveは一覧と片づけのコマンドだけを承認なしで許可し_resolve自体は許可しない():
    # resolve（上書き・削除）は戻せないので、承認なしの許可に含めない（実行のたびに確認が出る）
    assert allowed_commands("resolve") == ["conflicts", "prepare", "apply *", "release"]


def test_unwatchは停止と片づけと確認事項のコマンドだけを承認なしで許可する():
    assert allowed_commands("unwatch") == [
        "unwatch",
        "prepare",
        "apply *",
        "release",
        "record-stop",
    ]


def test_askの読み取りはvault_kb配下に限定する():
    tools = frontmatter(PLUGIN / "skills/ask/SKILL.md")["allowed-tools"].split()

    # 資料内の命令で、vault の外（~\.ssh など）を確認なしで読まれないように
    for tool in ("Read", "Grep", "Glob"):
        assert f"{tool}(//**/vault-kb/**)" in tools
        assert tool not in tools


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (
            "ask",
            ["Read(//**/vault-kb/**)", "Read(//**/references/reconcile.md)"],
        ),
        (
            "watch",
            ["Read(//**/vault-kb/.vault-kb-plan.json)", "Read(//**/references/reconcile.md)"],
        ),
        (
            "unwatch",
            ["Read(//**/vault-kb/.vault-kb-plan.json)", "Read(//**/references/reconcile.md)"],
        ),
        (
            "resolve",
            ["Read(//**/vault-kb/.vault-kb-plan.json)", "Read(//**/references/reconcile.md)"],
        ),
    ],
)
def test_Readの許可は決まった範囲だけで_括弧なしのツールを持たない(name, expected):
    tools = frontmatter(PLUGIN / f"skills/{name}/SKILL.md")["allowed-tools"].split()

    assert [t for t in tools if t.startswith("Read(")] == expected
    for bare in ("Read", "Write", "Edit"):
        assert bare not in tools


def test_initは診断と状態だけを承認なしで許可しSendMessageとListAgentsを持たない():
    tools = frontmatter(PLUGIN / "skills/init/SKILL.md")["allowed-tools"].split()

    assert allowed_commands("init") == ["doctor", "status"]
    assert "SendMessage" not in tools
    assert "ListAgents" not in tools


@pytest.mark.parametrize("name", ["ask", "resolve", "unwatch", "watch"])
def test_計画の書き込みはvault_kbの隠しファイルの計画だけを許可する(name):
    tools = frontmatter(PLUGIN / f"skills/{name}/SKILL.md")["allowed-tools"]

    # ~/.claude の下（${CLAUDE_PLUGIN_DATA}）は allowed-tools があっても確認が出るので、vault に置く
    assert re.findall(r"(?:Write|Edit)\([^)]*\)", tools) == [
        "Edit(//**/vault-kb/.vault-kb-plan.json)"
    ]


@pytest.mark.parametrize("name", ["ask", "resolve", "unwatch", "watch"])
def test_watchとaskとunwatchとresolveはreferencesのreconcileを読む(name):
    text = (PLUGIN / f"skills/{name}/SKILL.md").read_text(encoding="utf-8")

    assert "${CLAUDE_PLUGIN_ROOT}/references/reconcile.md" in text


def test_initは普段のセッションで打つ見張りの始め方を案内する():
    text = (PLUGIN / "skills/init/SKILL.md").read_text(encoding="utf-8")

    assert "`/vault-kb:watch` と打つ" in text
    assert "セッションを閉じると止まる" in text


SHIPPED_AND_DOCS = [
    *TEXT_FILES,
    *sorted((PLUGIN / "vault_kb").glob("*.py")),
    PLUGIN / "run.py",
    REPO / "README.md",
    *(REPO / "docs").glob("development.md"),  # docs/ は非公開。clone した環境にはない
]


@pytest.mark.parametrize("path", SHIPPED_AND_DOCS, ids=lambda p: p.parent.name + "/" + p.name)
def test_背景セッションの起動と許可の仕組みが残っていない(path):
    text = path.read_text(encoding="utf-8")

    for stale in (
        "--bg",
        r"\battach\b",  # 90_attachments は別物
        "--settings",
        "--allowedTools",
        "watch-settings",
        "claude agents",
        "watcher",
    ):
        assert not re.search(stale, text), stale


def test_watchはMonitorの張り方を持つ():
    text = (PLUGIN / "skills/watch/SKILL.md").read_text(encoding="utf-8")

    assert "timeout_ms: 1800000" in text
    assert 'run.py" --data "${CLAUDE_PLUGIN_DATA}" watch' in text


def test_フックはSessionStartでnotifyを15秒の上限つきで呼ぶ():
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))

    [entry] = hooks["hooks"]["SessionStart"]
    [hook] = entry["hooks"]
    assert hook["command"].endswith(' --data "${CLAUDE_PLUGIN_DATA}" notify')
    assert hook["timeout"] == 15


def test_フックはPreToolUseで片づけに使うツールだけにapproveを5秒の上限つきで呼ぶ():
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))

    [entry] = hooks["hooks"]["PreToolUse"]
    [hook] = entry["hooks"]
    assert entry["matcher"] == "Bash|Read|Write|Edit|Monitor"
    assert hook["command"] == (
        'python "${CLAUDE_PLUGIN_ROOT}/run.py" --data "${CLAUDE_PLUGIN_DATA}" approve'
    )
    assert hook["timeout"] == 5


def test_reconcileはrunpyのコマンドを1行そのままで実行させる():
    text = REFERENCES.read_text(encoding="utf-8")

    assert "1 行で" in text
    assert "足さない" in text


def test_reconcileの手順はapplyの失敗時にreleaseして止まる():
    text = REFERENCES.read_text(encoding="utf-8")

    assert "終了コード 2" in text
    assert 'run.py" --data "${CLAUDE_PLUGIN_DATA}" release' in text


def test_watchは見張りを張り直す前に片づけを1回行う():
    text = (PLUGIN / "skills/watch/SKILL.md").read_text(encoding="utf-8")

    assert "期限切れの通知が来たら、reconcile.md の手順を 1 回実行してから、手順 3" in text


@pytest.mark.parametrize("path", TEXT_FILES, ids=lambda p: p.parent.name + "/" + p.name)
def test_ハードコードしたパスとSKILL_DIRを書いていない(path):
    text = path.read_text(encoding="utf-8")

    assert not re.search(r"[A-Za-z]:[\\/]Users", text)
    assert "${CLAUDE_SKILL_DIR}" not in text


@pytest.mark.parametrize("path", TEXT_FILES, ids=lambda p: p.parent.name + "/" + p.name)
def test_CLAUDE_PLUGIN_ROOT配下の参照先が実在する(path):
    text = path.read_text(encoding="utf-8")

    referenced = re.findall(r"\$\{CLAUDE_PLUGIN_ROOT\}/([^\s\"'`\\]+)", text)

    assert referenced
    for rel in referenced:
        assert (PLUGIN / rel).is_file(), rel


@pytest.mark.parametrize("path", SKILLS, ids=lambda p: p.parent.name)
def test_SKILLに相対リンクを書いていない(path):
    assert not re.search(r"\]\((?!https?://|#)", path.read_text(encoding="utf-8"))


def test_MCPを同梱していない():
    assert not (PLUGIN / ".mcp.json").exists()
    plugin = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    market = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    assert "mcpServers" not in plugin
    assert all("mcpServers" not in entry for entry in market["plugins"])


def test_versionがメタとコードで一致する():
    plugin = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))

    assert plugin["version"] == __version__


def test_依存は全て固定されている():
    lines = (PLUGIN / "requirements.txt").read_text(encoding="utf-8").splitlines()

    assert lines
    assert all(re.fullmatch(r"[A-Za-z0-9_.\-]+==[0-9][^\s=]*", line) for line in lines if line)


@pytest.mark.skipif(
    shutil.which("claude") is None, reason="claude コマンドが PATH にない（環境要因）"
)
@pytest.mark.parametrize("target", [PLUGIN, REPO], ids=["plugin", "marketplace"])
def test_claude_plugin_validateがstrictで通る(target):
    result = subprocess.run(
        [shutil.which("claude"), "plugin", "validate", str(target), "--strict"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
