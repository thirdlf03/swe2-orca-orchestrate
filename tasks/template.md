# タスク仕様テンプレート — 候補ワーカー用

<!-- 使い方: {{...}} を埋めて `orch compete tasks/xxx.md -n 3` 等に渡す。
     「契約」セクションは全ワーカー共通で省略しないこと。 -->

## 目的
{{このタスクで実現すること。1〜3文で}}

## 成果物
{{作るもの。ファイルパス・機能・画面等を具体的に}}

## 制約・契約
- 作業は**自分の worktree** (`{{spawn時に自動で決まる}}`) 内で完結させること。
  他の worktree パス・ブランチを触らない。
- 変更は**自分のブランチにコミット**してから worker_done すること。
- 採用側が参照する本番ファイル: {{パス一覧}}
- 守るべき API/型の契約: {{契約ファイル or 記述}}
- write-scope: {{触ってよいパスの glob。外は読むだけ}}
- 検証コマンド(必須・全てパスすること):
  ```
  {{npx tsc --noEmit 等}}
  ```
- 審査用の確認手段: {{demoページ/スクショ手順/コマンド}}

## ライフサイクル義務(最重要 — プリアンブルと重複するが必須)
- **5分毎に heartbeat を送る**:
  `orca orchestration send --type heartbeat --task-id <ID> --dispatch-id <ID>`
- **仕様の意味が2通りに解釈できる・前提が壊れている時は `ask` で止まって聞く**:
  `orca orchestration ask --question "..." --timeout-ms 600000`
  自力推測で進むのは禁止(採否は内容で決まる。迷走成果は全て捨てられる)
- **ブロックされたら `escalation`** を送って待つ。無言で止まる/無言で迷走するのが最悪パターン
- 完了時は `worker_done` を**ちょうど1回**、明示的 `--outcome` 付きで:
  ```
  orca orchestration send --type worker_done --task-id <ID> --dispatch-id <ID> \
    --outcome succeeded --subject "<短い状態>" \
    --body "<3文要約: 何をしたか/検証結果/残課題>" \
    --files-modified "path/a,path/b" --report-path "<レポートを書いた場合そのパス>"
  ```
  失敗時は `--outcome failed`(proseで誤魔化さない)

## 報告に必ず含めるもの
- ブランチ名 / worktree パス / コミットハッシュ
- 検証コマンドの実結果(コマンドと終了コード)
- 仕様との意図的な乖離(あれば理由と一緒に)
- 自己完結レポート: `report.md` をブランチにコミット推奨

## その他
- 不採用時は成果物一式(worktree・ブランチ・生成ファイル)が機械的に削除される。
  採用に必要なものは全て自分のブランチにコミットしておくこと
- 検証は「通ったはず」ではなく実測で。スクショ・画面確認が可能なら自分で行う
- ツールhookにブロックされたら**同じことを別手段で再試行しない**
  (実測: 各セッションが平均3回以上同じブロックを踏む)。指示された代替
  (専用read/edit/writeツール、`workdir`パラメータ、イベント待機)をそのまま使う
- `cd DIR && cmd` はブロックされる。シェルの `workdir` パラメータを使う
- 待機が必要なら `sleep` ではなく `orca terminal wait` /
  `orca orchestration check --wait` / 条件ループを使う
