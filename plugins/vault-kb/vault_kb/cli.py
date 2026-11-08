"""コマンドライン。結果は JSON で標準出力に書く（watch だけは 1 行ずつの文字列）。

終了コード: 0 成功、1 実行時のエラー、2 引数・設定のエラー。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from . import bundle, lock, reconcile, watch, wiki
from .vault import (
    ConfigError,
    atomic_write,
    init_vault,
    install_obsidian_assets,
    load_vault,
    save_config,
)

_LOG_HINT = "処理した資料の記録は log.md にあります"
WIKI_PLAN_NAME = ".vault-kb-wiki-plan.json"  # vault の直下。wiki-apply が受け付ける唯一の計画


class _ApplyFailed(OSError):
    """reconcile.apply の中で起きた OSError（ログの場所を添えて報告する）。"""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vault_kb")
    sub = parser.add_subparsers(dest="command", required=True)
    configure = sub.add_parser("configure", help="vault を設定して標準のフォルダを作る")
    configure.add_argument("--vault", required=True)
    sub.add_parser("status", help="片づけ待ちの件数と、片づけの状態")
    sub.add_parser("prepare", help="追従・削除を反映し、判断が要る資料を変換して返す")
    apply = sub.add_parser("apply", help="計画（JSON）どおりに資料を置いてノートを書く")
    apply.add_argument("plan")
    wiki_check = sub.add_parser("wiki-check", help="統合ページ・テーマページ・保存した回答の状態")
    wiki_check.add_argument("plan")
    wiki_apply = sub.add_parser("wiki-apply", help="計画（JSON）どおりに .wiki/ へ書く")
    wiki_apply.add_argument("plan")
    sub.add_parser("reindex", help="index.md を作り直す")
    sub.add_parser("watch", help="vault の変化を 1 行ずつ出す（Ctrl+C か unwatch で終わる）")
    sub.add_parser("unwatch", help="見張りに停止の印を書き、確認事項を返す")
    sub.add_parser("record-stop", help="log.md に見張りの停止を記録する")
    return parser


def _data_dir() -> Path:
    home = os.environ.get("VAULT_KB_HOME")
    if not home:
        raise ConfigError("環境変数 VAULT_KB_HOME が未設定です（run.py の --data で渡します）")
    return Path(home)


def _configure(data_dir: Path, vault_arg: str) -> dict[str, Any]:
    root = Path(vault_arg).resolve()
    if not root.is_dir():
        raise ValueError(f"vault のフォルダが見つかりません: {root}")
    data_dir.mkdir(parents=True, exist_ok=True)
    vault = init_vault(root)
    install_obsidian_assets(root)
    save_config(data_dir, vault.root)
    bundle.write_indexes(vault)
    log = vault.root / "log.md"
    if not log.exists():
        atomic_write(log, "")
    return {"vault": str(vault.root)}


def _same_file(path: Path, expected: Path) -> bool:
    return path.resolve() == expected.resolve()


def _run(args: argparse.Namespace, data_dir: Path) -> dict[str, Any] | list[Any] | None:
    """サブコマンドを実行し、JSON にする結果を返す（watch は None）。"""
    if args.command == "configure":
        return _configure(data_dir, args.vault)
    vault = load_vault(data_dir)
    now = datetime.now().astimezone()
    if args.command == "status":
        return watch.status(vault, data_dir)
    if args.command == "prepare":
        try:
            return reconcile.prepare(vault, data_dir, now)
        except lock.Busy:
            return {"busy": True}
    if args.command == "apply":
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        try:
            return reconcile.apply(vault, data_dir, plan, now)
        except lock.Busy:
            return {"busy": True}
        except OSError as error:  # 後処理の失敗。処理した資料は log.md に残っている
            raise _ApplyFailed(str(error)) from error
    if args.command == "wiki-check":
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        requests = plan.get("requests") if isinstance(plan, dict) else None
        if not isinstance(requests, list) or not all(isinstance(r, dict) for r in requests):
            raise ValueError("requests は request の一覧で指定してください")
        return {"results": [wiki.check(vault, request) for request in requests]}
    if args.command == "wiki-apply":
        fixed = vault.root / WIKI_PLAN_NAME
        if not _same_file(Path(args.plan), fixed):
            raise ValueError(f"計画は vault 直下の {WIKI_PLAN_NAME} だけ指定できます: {args.plan}")
        plan = json.loads(fixed.read_text(encoding="utf-8"))
        if not isinstance(plan, dict):
            raise ValueError("計画は JSON のオブジェクトで指定してください")
        result = wiki.apply(vault, data_dir, plan, now)
        if "busy" not in result:
            fixed.unlink(missing_ok=True)
        return result
    if args.command == "reindex":
        bundle.write_indexes(vault)
        return {"reindexed": True}
    if args.command == "unwatch":
        watch.request_stop(data_dir, now)
        return {"stop_requested": True, **watch.open_items(vault, data_dir)}
    if args.command == "record-stop":
        unresolved = watch.open_items(vault, data_dir)["unresolved"]
        suffix = f"（未解消 {unresolved} 件）" if unresolved else ""
        bundle.append_log(vault, [f"**Stopped** 見張りを止めた{suffix}"], now)
        return {"recorded": True, "unresolved": unresolved}
    for line in watch.watch_events(vault, data_dir):  # watch
        print(line, flush=True)
    return None


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = _run(args, _data_dir())
    except KeyboardInterrupt:
        return 0
    except (ConfigError, ValueError) as error:  # JSON の構文エラーも ValueError
        print(f"エラー: {error}", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"実行時のエラー: {error}", file=sys.stderr)
        if isinstance(error, _ApplyFailed):
            print(_LOG_HINT, file=sys.stderr)
        return 1
    if result is not None:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
