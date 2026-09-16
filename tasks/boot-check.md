# タスク仕様 — boot経路スモークチェック(候補ワーカー用)

## 目的
`orch boot` 経路(Run bind → 分解 → compete投入 → spawn)のスモーク検証。
ワーカーが worktree・ブランチ・ライフサイクル義務込みで正しく起動し、
成果物をコミットして worker_done まで辿れることを確認する。

## 成果物
- `smoke/boot-check.md` を新規作成し、内容を次の1行のみとすること:
  ```
  boot経路で生成された
  ```

## 制約・契約
- 作業は**自分の worktree**(spawn時に自動で決まる)内で完結させること。
  他の worktree パス・ブランチを触らない。
- 変更は**自分のブランチにコミット**してから worker_done すること。
- 採用側が参照する本番ファイル: `smoke/boot-check.md`
- write-scope: `smoke/**`(それ以外は読むだけ・触らない)
- 検証コマンド(必須・パスすること):
  ```
  grep -qx 'boot経路で生成された' smoke/boot-check.md && git status --porcelain | wc -l
  ```
  (grep が終了コード0、かつ未コミット変更が0件であること)
- 審査用の確認手段: `cat smoke/boot-check.md` と `git log --oneline -1`

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
    --files-modified "smoke/boot-check.md" --report-path "<レポートを書いた場合そのパス>"
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
- 検証は「通ったはず」ではなく実測で
