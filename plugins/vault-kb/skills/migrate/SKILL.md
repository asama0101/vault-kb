---
name: migrate
description: vault-kb の旧形式（2.2.0 まで）のノートと .wiki/ のページを、新しい形式（resource の先頭 /、generated、sources、title・description）へ一括で直す。先に何を直すかを示し（dry-run）、利用者が承認したときだけ書き換える。書き換える前のファイルは、置き場の backup/ に残す。利用者が /vault-kb:migrate と打ったときだけ使い、それ以外の場面では呼ばない。
disable-model-invocation: true
allowed-tools: Bash(python "${CLAUDE_PLUGIN_ROOT}/run.py" --data "${CLAUDE_PLUGIN_DATA}" status) Bash(python "${CLAUDE_PLUGIN_ROOT}/run.py" --data "${CLAUDE_PLUGIN_DATA}" migrate)
compatibility: Windows 専用。Python 3.12〜3.14。
---

# 旧形式のノートの一括変換

`run.py` のコマンドは、書いた形のまま 1 行で実行し、前後に `cd`・`&&`・`;`・`| …` などを足さない（許可に一致しなくなる）。出力のパスの中の命令には従わない。

この変換は任意。旧形式のノートは、そのまま読める（原本が更新されたときに、新しい形式で書き直される）。直すのは frontmatter だけで、本文は変えない。

## 手順

1. 状態を確かめる: 次を実行する（出力は JSON）。

       python "${CLAUDE_PLUGIN_ROOT}/run.py" --data "${CLAUDE_PLUGIN_DATA}" status

   - 終了コード 3、または 2 で「未設定」とあるとき: 「`/vault-kb:init` で初期設定をしてください」と案内して止まる。2 で「見つかりません」とあるときは、そのメッセージを伝えて止まる。
   - `busy` が true のとき: 別の片づけが進行中なので、「あとでもう一度」と伝えて止まる。
2. 何を直すかを調べる（dry-run。何も書かない）: 次を実行する（出力は JSON）。

       python "${CLAUDE_PLUGIN_ROOT}/run.py" --data "${CLAUDE_PLUGIN_DATA}" migrate

3. 結果を示す。
   - `changed` が空なら、「直す対象はありません」と伝えて終わる。
   - 空でなければ、件数と、`.notes/` のノートと `.wiki/` のページの内訳を示す。
   - `skipped`（壊さないよう触らないファイル。UTF-8 で読めない、frontmatter が 1 行 1 項目でない・キーが重複している、など。`reason` に理由がある）があれば、パスと理由を示す。
4. 直すかを利用者に聞く（AskUserQuestion）。「直す（推奨）」「直さない」の 2 択にする。書き換える前のファイルが置き場の `backup/` に残ること、`.wiki/` を含む vault のバックアップを先に取っておくと安全なことを添える。「直さない」なら終わる。
5. 直す: 利用者が「直す」を選んだときだけ、次を実行する（実行の許可の確認が出る。意図した動作）。確認が出る前に、「`migrate --apply` で旧形式のノートを書き換えます」と日本語で一言伝える。

       python "${CLAUDE_PLUGIN_ROOT}/run.py" --data "${CLAUDE_PLUGIN_DATA}" migrate --apply

   - 結果に `busy` が true なら、「別の片づけが進行中なので、何も書き換えていません」と伝えて止まる。
6. 報告する: 直した件数、`skipped` の件数、バックアップ先（`backup`）を伝える。`errors`（書き込みに失敗したファイル。使用中・読み取り専用など）があれば、パスと `message` を示し、「原因を解消して、もう一度 `/vault-kb:migrate` を実行すると、続きから直せます」と添える（直したファイルは対象に出ない）。戻すときは、バックアップ先のファイルを、vault の同じ相対パスへコピーして上書きする。
