"""vault-kb の入口スクリプト。標準ライブラリだけで動く。

    python run.py [--data <置き場>] <サブコマンド> [引数...]

- doctor: 診断だけを行う（何も変更しない）。OS・Python・python コマンド・専用環境・vault を確かめ、
  足りないものには対処を表示する。
- setup [--yes]: 専用環境（置き場の venv）を作る。--yes なしは、作成内容を表示して止まる。
- approve: PreToolUse フック。stdin のツール呼び出しが vault-kb 自身の片づけの操作なら
  allow を出す。それ以外は何も出さない（通常の確認に任せる）。常に終了コード 0。
- それ以外（configure / status / prepare / apply / reindex / watch / unwatch /
  record-stop）:
  専用環境の Python で vault_kb を実行する。

置き場は、環境変数 VAULT_KB_HOME、次に --data の順で決める。
専用環境は、置き場の venv があればそれを使い、なければ実行中の Python に requirements.txt と
同じ版が揃っていればそれを使う。
終了コード: 0 成功、1 実行時のエラー、2 引数・設定のエラー、3 専用環境が使えない、
4 （doctor のみ）vault だけに問題がある。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REQUIREMENTS = ROOT / "requirements.txt"
RECONCILE = ROOT / "references" / "reconcile.md"
WIKI_GUIDE = ROOT / "references" / "wiki.md"
# requirements.txt の固定版が入る Python の範囲（numpy 2.5.4 は >=3.12、markitdown 0.1.9 は <3.15）
MIN_PYTHON = (3, 12)
MAX_PYTHON = (3, 15)  # この版は含まない
APPROX_SIZE = "約 300 MB"
MIN_FREE_MB = 500
PYPI_URL = "https://pypi.org/simple/"
PLAN_NAME = ".vault-kb-plan.json"
WIKI_PLAN_NAME = ".vault-kb-wiki-plan.json"  # vault_kb/cli.py の WIKI_PLAN_NAME と同じ値にする
# 確認なしで通す run.py のサブコマンド（init の setup --yes・configure は確認を出したまま）
UNATTENDED = {"status", "prepare", "watch", "unwatch", "record-stop"}


def _split_data(argv: list[str]) -> tuple[Path | None, list[str]]:
    """--data <パス> を引数から取り除き、置き場を決める（環境変数が優先）。"""
    rest, data = [], None
    it = iter(argv)
    for arg in it:
        if arg == "--data" and data is None:  # 最初の 1 つだけ。後ろに足された指定では差し替えない
            data = next(it, None)
        else:
            rest.append(arg)
    home = os.environ.get("VAULT_KB_HOME") or data
    return (Path(home) if home else None), rest


def _venv_python(data: Path) -> Path:
    return data / "venv" / "Scripts" / "python.exe"


def _marker(data: Path) -> Path:
    return data / "venv-requirements.sha256"


def _requirements_hash() -> str:
    return hashlib.sha256(REQUIREMENTS.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _venv_problem(data: Path) -> str | None:
    if not _venv_python(data).exists():
        return "専用環境が未作成です"
    marker = _marker(data)
    if not marker.exists() or marker.read_text(encoding="utf-8").strip() != _requirements_hash():
        return "requirements.txt と専用環境の版がずれています"
    return None


def _packages_problem(requirements: Path = REQUIREMENTS) -> str | None:
    """実行中の Python に、requirements.txt と同じ版のパッケージが揃っているか。"""
    from importlib import metadata  # approve（全ツール呼び出しで走る）を重くしない

    for line in requirements.read_text(encoding="utf-8").splitlines():
        name, _, pinned = line.partition("==")
        name, pinned = name.strip(), pinned.strip()
        if not pinned:
            continue
        try:
            installed = metadata.version(name)
        except metadata.PackageNotFoundError:
            return f"{name} が未導入です"
        if installed != pinned:
            return f"{name} の版が違います（固定 {pinned}、導入済み {installed}）"
    return None


def _runtime(data: Path) -> tuple[str | None, str | None, str | None]:
    """使う Python を決める。(種類, Python のパス, 問題)。venv が先、なければ実行中の Python。"""
    venv_problem = _venv_problem(data)
    if venv_problem is None:
        return "venv", str(_venv_python(data)), None
    if _python_problem(need_venv=False) is None and _packages_problem() is None:
        return "system", sys.executable, None
    return None, None, venv_problem


def _guide(data: Path, problem: str) -> str:
    return (
        f"専用環境が使えません: {problem}\n"
        f"使用中の Python: {sys.executable}\n"
        f"置き場: {data}\n"
        "利用者の承認を得て、`setup --yes` を実行してください。"
    )


def _python_problem(need_venv: bool = True) -> str | None:
    if not MIN_PYTHON <= tuple(sys.version_info)[:2] < MAX_PYTHON:
        low = ".".join(map(str, MIN_PYTHON))
        high = f"{MAX_PYTHON[0]}.{MAX_PYTHON[1] - 1}"
        return f"Python {low}〜{high} が必要です（現在 {sys.version.split()[0]}）"
    for module in ("venv", "ensurepip") if need_venv else ():
        if importlib.util.find_spec(module) is None:
            return f"Python の {module} モジュールが使えません"
    return None


def _pypi_problem() -> str | None:
    import urllib.request  # approve（全ツール呼び出しで走る）を重くしない

    request = urllib.request.Request(PYPI_URL, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=5):  # noqa: S310  固定の https の URL
            return None
    except OSError as error:  # URLError・タイムアウトを含む
        return (
            f"PyPI に届きません（{error}）。"
            "社内ネットワークでは、プロキシの設定（環境変数 HTTPS_PROXY）が要ることがあります。"
            "pip の設定（pip.ini の index-url）で社内ミラーを使っているなら、この NG は無視してよい"
        )


def _disk_problem(path: Path) -> str | None:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent  # 未作成の置き場は、存在する親のドライブで測る
    try:
        free_mb = shutil.disk_usage(probe).free // (1024 * 1024)
    except OSError as error:  # 存在しないドライブなど
        return f"空き容量を調べられません（{error}）"
    if free_mb < MIN_FREE_MB:
        return f"空き容量が足りません（{free_mb} MB。{MIN_FREE_MB} MB 以上が必要）"
    return None


def _configured_vault(data: Path) -> tuple[Path | None, str | None]:
    """(設定されている vault のパス, 問題)。未設定・壊れていれば、パスは None。"""
    path = data / "config.json"
    if not path.exists():
        return None, "vault が未設定です"
    try:
        configured = json.loads(path.read_text(encoding="utf-8"))["vault"]
        if not configured:  # Path("") は「現在のフォルダ」になってしまう
            raise ValueError(configured)
        return Path(configured), None
    except (OSError, ValueError, KeyError, TypeError):
        return None, f"{path} が壊れています"


def _vault_problem(data: Path) -> tuple[str | None, str]:
    """(問題, 設定されている vault のパス)。"""
    root, problem = _configured_vault(data)
    if root is None:
        return problem, ""
    if not root.is_dir():
        return (
            f"vault のフォルダが見つかりません（別の PC・外付けドライブ・同期の切断など）: {root}",
            "",
        )
    return None, str(root)


def _show(name: str, problem: str | None, remedy: list[str], ok_detail: str = "") -> bool:
    """診断の 1 行を出す。問題があれば、対処の行を続ける。問題があったかを返す。"""
    if problem is None:
        print(f"OK  {name}" + (f": {ok_detail}" if ok_detail else ""))
        return False
    print(f"NG  {name}: {problem}")
    for line in remedy:
        print(f"      {line}")
    return True


def _hook_python() -> tuple[str, str | None]:
    """(`python` コマンドが指す Python の実体, 問題)。フックと同じ `python` を実際に起動する。"""
    found = shutil.which("python")
    if found is None:
        return "", "PATH に python がありません（フックが python で起動する）"
    try:
        done = subprocess.run(
            [found, "-c", "import sys; print(sys.executable)"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            env={**os.environ, "PYTHONUTF8": "1"},
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        done = None
    executable = done.stdout.strip() if done and done.returncode == 0 else ""
    if not executable:
        return "", f"python を起動できません（Microsoft Store の案内だけが出る状態など）: {found}"
    return executable, None


def _doctor(data: Path) -> int:
    run_py = f'python "{ROOT / "run.py"}" --data "{data.absolute()}"'
    kind, _, runtime_problem = _runtime(data)
    print(f"使用中の Python: {sys.executable}\n置き場: {data}")

    hook_python, hook_problem = _hook_python()
    python_remedy = [
        "Python のインストーラの「Add python.exe to PATH」を入れて入れ直すか、",
        "環境変数 PATH に python.exe のあるフォルダを足す。新しいターミナルで確かめる。",
    ]
    if hook_problem is None and kind == "system" and not _same(hook_python, Path(sys.executable)):
        hook_problem = (
            f"フックが使う python は {hook_python}。専用環境（システムの Python）は "
            f"{sys.executable} で、別の Python です"
        )
        python_remedy = [
            "python コマンドで doctor を実行し直す（python コマンドのほうが使われる）:"
        ]
        python_remedy.append(f"  {run_py} doctor")
    runtime_name = "専用環境"
    if runtime_problem:
        system_problem = _python_problem(need_venv=False) or _packages_problem()
        runtime_problem = f"{runtime_problem}（システムの Python: {system_problem}）"
    else:
        runtime_name += "（venv）" if kind == "venv" else "（システムの Python）"
    env_ng = [
        _show(
            "OS",
            None if sys.platform == "win32" else f"Windows 専用です（現在 {sys.platform}）",
            [],
        ),
        _show(
            "Python",
            _python_problem(need_venv=kind != "system"),
            [
                "Python 3.12〜3.14 を入れる（python.org か winget install Python.Python.3.13）",
                "入れたあと、新しいターミナルで python --version を確かめる。",
            ],
        ),
        _show("python コマンド", hook_problem, python_remedy, ok_detail=hook_python),
        _show(
            runtime_name,
            runtime_problem,
            [
                "対処（実行の承認が要る）:",
                f"  {run_py} setup --yes",
                "Claude Code では /vault-kb:init で実行できる。",
            ],
        ),
    ]
    if runtime_problem:  # 作るときに要るものだけを確かめる（毎回の待ちを避ける）
        env_ng.append(_show("PyPI への接続", _pypi_problem(), []))
        env_ng.append(
            _show("空き容量", _disk_problem(data), ["置き場のあるドライブに空きを作る。"])
        )
    vault_problem, vault_path = _vault_problem(data)
    vault_ng = _show(
        "vault",
        vault_problem,
        [
            "Claude Code で /vault-kb:init を実行して設定する（パスを聞かれる）。",
            f'ターミナルでは: {run_py} configure --vault "<vault のパス>"',
        ],
        ok_detail=vault_path,
    )
    if any(env_ng):
        return 3
    return 4 if vault_ng else 0


def _requirement_count() -> int:
    return sum(1 for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines() if line.strip())


def _setup(data: Path, approved: bool) -> int:
    if not approved:
        print(
            "専用環境を作成します（利用者の承認が必要です）。\n"
            f"- 作成先: {data / 'venv'}\n"
            f"- 内容: markitdown と依存ライブラリ（{_requirement_count()} 件。PyPI から取得）\n"
            f"- 容量: {APPROX_SIZE}\n"
            f"- 使う Python: {sys.executable}\n"
            "承認する場合は、`setup --yes` を付けて再実行してください。"
        )
        return 3
    if problem := _python_problem():
        print(_guide(data, problem), file=sys.stderr)
        return 3
    venv_dir = data / "venv"
    data.mkdir(parents=True, exist_ok=True)
    try:
        # cwd は置き場にする（-m は cwd の同名モジュールを先に読むため、利用者のフォルダを避ける）
        if not _venv_python(data).exists():
            subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True, cwd=data)
        subprocess.run(
            [
                str(_venv_python(data)),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "-r",
                str(REQUIREMENTS),
            ],
            check=True,
            cwd=data,
        )
    except subprocess.CalledProcessError as error:
        print(f"専用環境の作成に失敗しました（終了コード {error.returncode}）", file=sys.stderr)
        return 1
    _marker(data).write_text(_requirements_hash(), encoding="utf-8")
    print(f"専用環境を作成しました: {venv_dir}")
    return 0


def _plan_path(data: Path) -> Path | None:
    """設定済みの vault の計画ファイル。未設定・壊れていれば None。"""
    root, _ = _configured_vault(data)
    return root / PLAN_NAME if root is not None else None


def _wiki_plan_path(data: Path) -> Path | None:
    """設定済みの vault の wiki 計画ファイル。未設定・壊れていれば None。"""
    root, _ = _configured_vault(data)
    return root / WIKI_PLAN_NAME if root is not None else None


def _same(path: str | Path, expected: Path) -> bool:
    """同じファイルを指すか（`\\`・`/`・大文字小文字・ジャンクションの違いを吸収する）。"""
    return Path(path).resolve() == expected.resolve()


def _is_own_command(
    command: str,
    data: Path,
    plan: Path | None,
    subs: set[str],
    wiki_plan: Path | None = None,
) -> bool:
    """`python "<ROOT>/run.py" --data "<DATA>" <サブ> [計画]` の 1 コマンドだけに完全一致するか。"""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if len(tokens) < 5 or tokens[0] != "python" or tokens[2] != "--data":
        return False
    # シェルが展開する文字と改行は不可。パスの .. は resolve が文字列のまま畳むので不可
    if any(c in token for token in tokens for c in "$`\n\r"):
        return False
    if any(".." in Path(token).parts for token in (tokens[1], tokens[3], *tokens[5:])):
        return False
    if not _same(tokens[1], ROOT / "run.py") or not _same(tokens[3], data):
        return False
    sub, args = tokens[4], tokens[5:]
    if sub == "apply":
        return "apply" in subs and plan is not None and len(args) == 1 and _same(args[0], plan)
    if sub in ("wiki-check", "wiki-apply"):
        return (
            sub in subs and wiki_plan is not None and len(args) == 1 and _same(args[0], wiki_plan)
        )
    return sub in subs and not args


def _approve(data: Path) -> int:
    """PreToolUse フック。自分の片づけの操作だけ allow を出す。何があっても黙って 0 で終わる。"""
    try:
        sys.stdin.reconfigure(encoding="utf-8")
        payload = json.loads(sys.stdin.read())
        tool, tool_input = payload["tool_name"], payload["tool_input"]
        plan = _plan_path(data)
        wiki_plan = _wiki_plan_path(data)
        if tool == "Bash":
            subs = UNATTENDED | {"apply", "wiki-check", "wiki-apply"}
            allowed = _is_own_command(tool_input["command"], data, plan, subs, wiki_plan)
        elif tool == "Monitor":
            allowed = _is_own_command(tool_input["command"], data, plan, {"watch"})
        elif tool in ("Read", "Write", "Edit"):
            path = tool_input["file_path"]
            allowed = (
                (plan is not None and _same(path, plan))
                or (wiki_plan is not None and _same(path, wiki_plan))
                or (tool == "Read" and (_same(path, RECONCILE) or _same(path, WIKI_GUIDE)))
            )
        else:
            allowed = False
    except Exception:  # noqa: BLE001  フックの失敗で、利用者のセッションを妨げない
        return 0
    if allowed:
        decision = {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": "vault-kb の片づけ",
        }
        print(json.dumps({"hookSpecificOutput": decision}, ensure_ascii=False))
    return 0


def _delegate(data: Path, args: list[str]) -> int:
    _, python, problem = _runtime(data)
    if problem:
        print(_guide(data, problem), file=sys.stderr)
        return 3
    env = {**os.environ, "VAULT_KB_HOME": str(data), "PYTHONUTF8": "1"}
    return subprocess.run([python, str(ROOT / "kb.py"), *args], env=env, check=False).returncode


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")  # 日本語 Windows の既定（cp932）で文字化けさせない
    data, args = _split_data(argv)
    if args[:1] == ["approve"]:
        return _approve(data) if data else 0
    if data is None:
        print(
            "置き場が決まりません。環境変数 VAULT_KB_HOME か、"
            "`--data <置き場>`（スキルでは ${CLAUDE_PLUGIN_DATA}）を指定してください。",
            file=sys.stderr,
        )
        return 2
    if not args:
        print(
            "サブコマンドを指定してください（doctor / setup / status / prepare / ...）",
            file=sys.stderr,
        )
        return 2
    if args[0] == "doctor":
        return _doctor(data)
    if args[0] == "setup":
        rest = args[1:]
        if set(rest) - {"--yes"}:
            print(
                f"setup の引数は `--yes` だけです（受け取った引数: {' '.join(rest)}）",
                file=sys.stderr,
            )
            return 2
        return _setup(data, approved="--yes" in rest)
    return _delegate(data, args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
