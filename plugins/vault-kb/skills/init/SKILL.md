---
name: init
description: vault-kb の初回導入と設定。専用の Python 環境（約 300 MB）を作り、Obsidian の vault（`vault-kb` フォルダ）を指定して中のフォルダを作り、見張りの始め方を案内する。設定の変更（vault を変える・環境を作り直す）もここで行う。「vault-kb を使い始めたい」「初期設定して」「vault を変えたい」「専用環境を作り直して」と頼まれたとき、または他のスキルが未設定と案内したときに使う。資料の片づけの見張りには watch、検索・質問には ask を使う。
disable-model-invocation: true
allowed-tools: Read Bash(python "${CLAUDE_PLUGIN_ROOT}/run.py" --data "${CLAUDE_PLUGIN_DATA}" doctor) Bash(python "${CLAUDE_PLUGIN_ROOT}/run.py" --data "${CLAUDE_PLUGIN_DATA}" status)
compatibility: Windows 専用。Python 3.10 以上。
---

# 初回導入と設定

以降の `run.py` は、次の形で実行する。出力は JSON（`doctor` と `setup` は文章）。

    python "${CLAUDE_PLUGIN_ROOT}/run.py" --data "${CLAUDE_PLUGIN_DATA}" <サブコマンド>

終了コード: 0 成功、1 実行時のエラー、2 設定・入力のエラー、3 専用環境が使えない、4 （`doctor` のみ）vault だけに問題がある。

`setup --yes` と `configure` は、実行の許可を求める確認が出る（意図した動作）。確認が出る前に、「次に何を許可するのか」（例: 専用環境を作成します・vault を設定して中のフォルダを作ります）を日本語で一言伝える。

## 手順

1. 診断する: `doctor` を実行する。`NG` の行と、その下の対処を、利用者に示す。
   - 終了コード 0: 専用環境は使える（置き場の venv か、システムの Python のどちらか。表示で分かる）。手順 3 へ。
   - 終了コード 3: 使えないものがある。OS・Python・`python` コマンドが NG なら、対処を示して止まる（直ったら、もう一度 `/vault-kb:init`）。空き容量が NG のときも、対処を示して止まる。これらが OK で `専用環境` が NG なら手順 2 へ進む（`PyPI への接続` だけが NG のときは、pip の設定で社内ミラーを使っていることがあるので、利用者に確かめ、使っていれば手順 2 へ進む）。
   - 終了コード 4: 専用環境は使える。vault だけが未設定か使えない。手順 3 へ。
   - 終了コード 1 や 2: エラーを報告して止まる。
2. 専用環境を作る: 入れ先を利用者に聞く。venv（推奨。他のツールと分かれる）か、システムの Python（venv を作らない。他のツールが使う同名のパッケージの版が変わることがある）。選ばれた方で `setup --mode venv`（または `--mode system`）を `--yes` なしで実行して、入れ先・容量（約 300 MB）・取得元・注意を利用者に示し、承認を得てから `--yes` を付けて実行する。承認が得られないときは止まる。失敗したときは、メッセージを報告して止まる（Python 3.10 以上が `python` で起動できること）。
3. vault を確かめる: `status` を実行する。
   - 終了コード 0: 設定済み。`vault` のパスを示して、変更したいかを聞く。変更しないなら手順 5 へ。
   - 終了コード 2（未設定）、または変更したいとき: 手順 4 へ。
4. vault を設定する: 利用者に、vault（Obsidian で開く `vault-kb` フォルダ）の絶対パスを聞く。フォルダがなければ、パスの確認を求める。フォルダの名前が `vault-kb` でないと、`ask` と `watch` の読み取りと計画ファイルの編集に、許可の確認が出ることを伝える。`configure --vault "<パス>"` を実行する。vault の中に、`00_inbox/`・`10_daily/`・`20_projects/`・`30_shared/99_その他/`・`40_knowledge/99_その他/`・`50_prompts/`・`80_archive/`・`90_attachments/`・`index.md`・`log.md` が作られ、`91_templates/`（ノートのテンプレート 7 種類）と `92_bases/`（その日のノート.base）に、同名のファイルがなければ置かれる（既存は上書きしない）。vault を変えるときは、前の vault のファイルは動かないことを伝える。
5. 動作を確かめる: `doctor` と `status` を実行し、結果（`vault` のパス、`pending`）を報告する。
6. 使い方を案内する。
   - 資料は `vault` の `00_inbox` フォルダに置く。完了した案件は、`20_projects` から `80_archive` へ、エクスプローラーか Obsidian で動かす。
   - Obsidian の設定値の一覧は、GitHub の README の「Obsidian の設定」にある。テンプレートを使うには、コアプラグイン「テンプレート」のテンプレートフォルダの場所を `91_templates` にする。
   - 見張り: 普段使うセッション（この会話でもよい）で `/vault-kb:watch` と打つと、そのセッションが見張りになり、`vault-kb` フォルダに変化があるたびに、確認なしで案件・区分のフォルダへ片づける（許可はプラグインのフックが与える。利用者の設定は要らない）。セッションを閉じると止まるので、開き直したら、また `/vault-kb:watch` と打つ。止まっている間に置いた資料は、次の `/vault-kb:watch` か `/vault-kb:ask` で片づく。
   - 探す・質問するのは `/vault-kb:ask`（または、そのまま質問する）。
   - 片づけが間違っていたときは、Obsidian かエクスプローラーで、資料を正しいフォルダへ手で動かす。
