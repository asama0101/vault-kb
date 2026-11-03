"""vault-kb の入口スクリプト。標準ライブラリだけで動く。

    python run.py [--data <置き場>] <サブコマンド> [引数...]

- doctor: 診断だけを行う（何も変更しない）。
- setup [--yes]: 専用環境（venv）を作る。--yes なしは、作成内容を表示して止まる。
- approve: PreToolUse フック。stdin のツール呼び出しが vault-kb 自身の片づけの操作なら
  allow を出す。それ以外は何も出さない（通常の確認に任せる）。常に終了コード 0。
- それ以外（configure / status / notify / prepare / apply / release / reindex / watch / unwatch /
  record-stop）:
  専用環境の Python で vault_kb を実行する。

置き場は、環境変数 VAULT_KB_HOME、次に --data の順で決める。
終了コード: 0 成功、1 実行時のエラー、2 引数・設定のエラー、3 専用環境が使えない。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REQUIREMENTS = ROOT / "requirements.txt"
RECONCILE = ROOT / "references" / "reconcile.md"
MIN_PYTHON = (3, 10)
APPROX_SIZE = "約 300 MB"
PLAN_NAME = ".vault-kb-plan.json"
# 確認なしで通す run.py のサブコマンド（init の setup --yes・configure は確認を出したまま）
UNATTENDED = {"status", "prepare", "release", "watch", "unwatch", "record-stop"}


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


def _env_problem(data: Path) -> str | None:
    if not _venv_python(data).exists():
        return "専用環境が未作成です"
    marker = _marker(data)
    if not marker.exists() or marker.read_text(encoding="utf-8").strip() != _requirements_hash():
        return "requirements.txt と専用環境の版がずれています"
    return None


def _guide(data: Path, problem: str) -> str:
    return (
        f"専用環境が使えません: {problem}\n"
        f"使用中の Python: {sys.executable}\n"
        f"置き場: {data}\n"
        "利用者の承認を得て、`setup --yes` を実行してください。"
    )


def _python_problem() -> str | None:
    if sys.version_info < MIN_PYTHON:
        need = ".".join(map(str, MIN_PYTHON))
        return f"Python {need} 以上が必要です（現在 {sys.version.split()[0]}）"
    for module in ("venv", "ensurepip"):
        if importlib.util.find_spec(module) is None:
            return f"Python の {module} モジュールが使えません"
    return None


def _doctor(data: Path) -> int:
    checks = [
        ("Python", _python_problem()),
        ("専用環境", _env_problem(data)),
    ]
    print(f"使用中の Python: {sys.executable}\n置き場: {data}")
    for name, problem in checks:
        print(f"{'NG' if problem else 'OK'}  {name}" + (f": {problem}" if problem else ""))
    return 3 if any(problem for _, problem in checks) else 0


def _setup(data: Path, approved: bool) -> int:
    requirements = [
        line for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    if not approved:
        print(
            "専用環境を作成します（利用者の承認が必要です）。\n"
            f"- 作成先: {data / 'venv'}\n"
            f"- 内容: markitdown と依存ライブラリ（{len(requirements)} 件。PyPI から取得）\n"
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
    try:
        root = json.loads((data / "config.json").read_text(encoding="utf-8"))["vault"]
        return Path(root) / PLAN_NAME
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _same(path: str | Path, expected: Path) -> bool:
    """同じファイルを指すか（`\\`・`/`・大文字小文字・ジャンクションの違いを吸収する）。"""
    return Path(path).resolve() == expected.resolve()


def _is_own_command(command: str, data: Path, plan: Path | None, subs: set[str]) -> bool:
    """`python "<ROOT>/run.py" --data "<DATA>" <サブ> [計画]` の 1 コマンドだけに完全一致するか。"""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if len(tokens) < 5 or tokens[0] != "python" or tokens[2] != "--data":
        return False
    if not _same(tokens[1], ROOT / "run.py") or not _same(tokens[3], data):
        return False
    sub, args = tokens[4], tokens[5:]
    if sub == "apply":
        return "apply" in subs and plan is not None and len(args) == 1 and _same(args[0], plan)
    return sub in subs and not args


def _approve(data: Path) -> int:
    """PreToolUse フック。自分の片づけの操作だけ allow を出す。何があっても黙って 0 で終わる。"""
    try:
        sys.stdin.reconfigure(encoding="utf-8")
        payload = json.loads(sys.stdin.read())
        tool, tool_input = payload["tool_name"], payload["tool_input"]
        plan = _plan_path(data)
        if tool == "Bash":
            allowed = _is_own_command(tool_input["command"], data, plan, UNATTENDED | {"apply"})
        elif tool == "Monitor":
            allowed = _is_own_command(tool_input["command"], data, plan, {"watch"})
        elif tool in ("Read", "Write", "Edit"):
            path = tool_input["file_path"]
            allowed = (plan is not None and _same(path, plan)) or (
                tool == "Read" and _same(path, RECONCILE)
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
    problem = _env_problem(data)
    if problem:
        if args[0] == "notify":  # セッション開始のフックは、未導入でも成功させて、init を案内する
            print(f"[vault-kb] {problem}。/vault-kb:init で設定してください。")
            return 0
        print(_guide(data, problem), file=sys.stderr)
        return 3
    env = {**os.environ, "VAULT_KB_HOME": str(data), "PYTHONUTF8": "1"}
    return subprocess.run(
        [str(_venv_python(data)), str(ROOT / "kb.py"), *args], env=env, check=False
    ).returncode


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
        return _setup(data, approved="--yes" in args[1:])
    return _delegate(data, args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
