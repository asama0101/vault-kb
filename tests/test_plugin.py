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
WIKI_GUIDE = PLUGIN / "references" / "wiki.md"
TEXT_FILES = [*SKILLS, REFERENCES, WIKI_GUIDE, PLUGIN / "hooks" / "hooks.json"]


def frontmatter(path: Path) -> dict[str, str]:
    head = path.read_text(encoding="utf-8").split("---\n")[1]
    return dict(line.split(": ", 1) for line in head.splitlines() if ": " in line)


def test_スキルは6つあり_nameがディレクトリ名と一致する():
    assert [p.parent.name for p in SKILLS] == [
        "ask",
        "init",
        "lint",
        "sweep",
        "unwatch",
        "watch",
    ]
    for skill in SKILLS:
        meta = frontmatter(skill)
        assert meta["name"] == skill.parent.name
        assert meta["description"]


def test_resolveスキルはない():
    assert not (PLUGIN / "skills" / "resolve").exists()


def test_手順書は同名のconflictだけの結果で繰り返さない():
    text = REFERENCES.read_text(encoding="utf-8")

    assert "（`filed`・`refreshed`・`discarded`）が 1 件以上" in text
    assert "`discarded`・`conflict`）" not in text


def test_init_lint_sweep_unwatch_watchだけが自動起動を禁止している():
    for name in ("init", "lint", "sweep", "unwatch", "watch"):
        assert frontmatter(PLUGIN / f"skills/{name}/SKILL.md")["disable-model-invocation"] == "true"
    assert "disable-model-invocation" not in frontmatter(PLUGIN / "skills/ask/SKILL.md")


def allowed_commands(skill: str) -> list[str]:
    """allowed-tools の run.py コマンド（サブコマンドと引数のパターン）。"""
    tools = frontmatter(PLUGIN / f"skills/{skill}/SKILL.md")["allowed-tools"]
    return re.findall(r'run\.py" --data "\$\{CLAUDE_PLUGIN_DATA\}" ([^)]+)\)', tools)


def test_askは状態確認と片づけのコマンドだけを承認なしで許可する():
    # wiki のコマンドは test_askとlintはwikiのコマンドだけを足す で見る
    cleanup = [c for c in allowed_commands("ask") if not c.startswith("wiki-")]

    assert cleanup == ["status", "prepare", "apply *"]


@pytest.mark.parametrize("name", ["ask", "lint"])
def test_askとlintはwikiのコマンドだけを足す(name):
    wiki = [c for c in allowed_commands(name) if c.startswith("wiki-")]

    assert wiki == ["wiki-check *", "wiki-apply *"]


LINT_TOOLS = [
    "Read(//**/vault-kb/**)",
    "Grep(//**/vault-kb/**)",
    "Glob(//**/vault-kb/**)",
    *(
        f'Bash(python "${{CLAUDE_PLUGIN_ROOT}}/run.py" --data "${{CLAUDE_PLUGIN_DATA}}" {sub})'
        for sub in ("status", "prepare", "apply *", "wiki-check *", "wiki-apply *")
    ),
    "Read(//**/references/reconcile.md)",
    "Read(//**/references/wiki.md)",
    "Edit(//**/vault-kb/.vault-kb-plan.json)",
    "Edit(//**/vault-kb/.vault-kb-wiki-plan.json)",
]


def test_lintの許可は決まった範囲だけ():
    tools = frontmatter(PLUGIN / "skills/lint/SKILL.md")["allowed-tools"]

    # 空白を含む Bash(...) を 1 つの許可として切り出す
    assert re.findall(r"\w+\([^)]*\)", tools) == LINT_TOOLS
    assert re.sub(r"\w+\([^)]*\)", "", tools).strip() == ""


def test_askの許可はlintと同じ():
    tools = frontmatter(PLUGIN / "skills/ask/SKILL.md")["allowed-tools"]

    assert re.findall(r"\w+\([^)]*\)", tools) == LINT_TOOLS


def test_lintは自動起動しない():
    meta = frontmatter(PLUGIN / "skills/lint/SKILL.md")

    assert meta["disable-model-invocation"] == "true"


def test_watchは片づけのコマンドとMonitorだけを許可する():
    tools = frontmatter(PLUGIN / "skills/watch/SKILL.md")["allowed-tools"].split()

    assert allowed_commands("watch") == ["prepare", "apply *"]
    assert "Monitor" in tools


def test_sweepは状態確認と片づけのコマンドだけを承認なしで許可する():
    # unwatch は動いている見張りを止めるので、sweep には許可しない
    assert allowed_commands("sweep") == ["status", "prepare", "apply *"]


def test_unwatchは停止と片づけと確認事項のコマンドだけを承認なしで許可する():
    assert allowed_commands("unwatch") == [
        "unwatch",
        "prepare",
        "apply *",
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
            [
                "Read(//**/vault-kb/**)",
                "Read(//**/references/reconcile.md)",
                "Read(//**/references/wiki.md)",
            ],
        ),
        (
            "lint",
            [
                "Read(//**/vault-kb/**)",
                "Read(//**/references/reconcile.md)",
                "Read(//**/references/wiki.md)",
            ],
        ),
        (
            "watch",
            ["Read(//**/vault-kb/.vault-kb-plan.json)", "Read(//**/references/reconcile.md)"],
        ),
        (
            "sweep",
            ["Read(//**/vault-kb/.vault-kb-plan.json)", "Read(//**/references/reconcile.md)"],
        ),
        (
            "unwatch",
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


PLAN_EDIT = "Edit(//**/vault-kb/.vault-kb-plan.json)"
WIKI_PLAN_EDIT = "Edit(//**/vault-kb/.vault-kb-wiki-plan.json)"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("ask", [PLAN_EDIT, WIKI_PLAN_EDIT]),
        ("lint", [PLAN_EDIT, WIKI_PLAN_EDIT]),
        ("sweep", [PLAN_EDIT]),
        ("unwatch", [PLAN_EDIT]),
        ("watch", [PLAN_EDIT]),
    ],
)
def test_計画の書き込みはvault_kbの隠しファイルの計画だけを許可する(name, expected):
    tools = frontmatter(PLUGIN / f"skills/{name}/SKILL.md")["allowed-tools"]

    # ~/.claude の下（${CLAUDE_PLUGIN_DATA}）は allowed-tools があっても確認が出るので、vault に置く
    assert re.findall(r"(?:Write|Edit)\([^)]*\)", tools) == expected


@pytest.mark.parametrize("name", ["ask", "lint", "sweep", "unwatch", "watch"])
def test_watchとaskとunwatchとsweepとlintはreferencesのreconcileを読む(name):
    text = (PLUGIN / f"skills/{name}/SKILL.md").read_text(encoding="utf-8")

    assert "${CLAUDE_PLUGIN_ROOT}/references/reconcile.md" in text


@pytest.mark.parametrize("name", ["ask", "lint"])
def test_askとlintはreferencesのwikiを読む(name):
    text = (PLUGIN / f"skills/{name}/SKILL.md").read_text(encoding="utf-8")

    assert "${CLAUDE_PLUGIN_ROOT}/references/wiki.md" in text


def test_wiki_mdは参照先が実在しTEXT_FILESの規則に通る():
    # 参照先の実在・パスの規則は TEXT_FILES の parametrize のテストが見る
    assert WIKI_GUIDE.is_file()
    assert WIKI_GUIDE in TEXT_FILES
    # 禁じる語の検査が wiki.py にも及ぶ
    assert PLUGIN / "vault_kb" / "wiki.py" in SHIPPED_AND_DOCS


def test_wiki_mdはwikiのコマンドを計画の固定パスで1行そのままで実行させる():
    text = WIKI_GUIDE.read_text(encoding="utf-8")

    for sub in ("wiki-check", "wiki-apply"):
        assert (
            f'python "${{CLAUDE_PLUGIN_ROOT}}/run.py" --data "${{CLAUDE_PLUGIN_DATA}}" {sub} '
            '"<vault>/.vault-kb-wiki-plan.json"'
        ) in text
    assert "1 行で" in text
    assert "足さない" in text
    assert "別の実行に使われた" in text
    assert '{"busy": true}' in text
    assert "終了コード 2" in text


def test_wiki_mdはwikiのコードの項目名で書く():
    text = WIKI_GUIDE.read_text(encoding="utf-8")

    # vault_kb/wiki.py の check・apply の入出力の名前（変えたら手順書も直す）
    for field in (
        "read_list",
        "deferred",
        "unread",
        "digest",
        "expected_digest",
        "broken",
        "reusable",
        "deselected",
        "unknown",
        "selected",
        "evidence",
        "scope",
        "overwrite",
        "slug",
    ):
        assert f"`{field}`" in text, field
    assert '"full": true' in text
    assert "~~旧~~（取り消し 日付: 理由）" in text


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


def test_フックはPreToolUseだけを持つ():
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))

    assert list(hooks["hooks"]) == ["PreToolUse"]


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


def test_reconcileの手順はapplyの失敗時に報告して止まり_busyなら何もしない():
    text = REFERENCES.read_text(encoding="utf-8")

    assert "終了コード 2" in text
    assert "release" not in text
    assert "lock_token" not in text
    assert '{"busy": true}' in text


def test_reconcileの手順は別の実行に計画を使われたapplyの失敗で闇雲に再実行しない():
    text = REFERENCES.read_text(encoding="utf-8")

    assert "別の実行に使われた" in text
    assert "次の `prepare` が拾う" in text


def test_reconcileの手順はconflictだけが残るときmoreでも繰り返さず報告する():
    text = REFERENCES.read_text(encoding="utf-8")

    assert "すべて `conflict`" in text
    assert "同名のファイルが `00_inbox` に N 件残り" in text


@pytest.mark.parametrize("skill", ["watch", "sweep", "ask", "unwatch", "init", "lint"])
def test_スキルのallowed_toolsにreleaseがない(skill):
    tools = frontmatter(PLUGIN / f"skills/{skill}/SKILL.md")["allowed-tools"]

    assert "release" not in tools


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
