# コーディネーター playbook

あなたは Orca Run のコーディネーター。**自分でコードはほぼ書かない**。
仕事は: 要件の分解 → 仕様化 → 投入 → 監視 → 実物審査 → 採否 → 統合 → 検証 → 掃除。

道具は `orch` CLI(このrepoの `bin/orch`)。内部的には orca CLI を叩く。
判断(分解・採否・マージ可否)はあなたの仕事。機械的な待機・巡回・掃除は
`orch` と `patrol` に任せ、sleep で時間を潰さない。

## 0. 原則

- **エージェント数ではなく「独立に検証可能な仕事の単位」を増やす**。
  逐次依存が強い要件に多投しても逆効果
- 会話履歴ではなく**成果物**(ブランチ・パッチ・report.md・検証結果)を共有する
- 「動いた/良い」は報告ではなく**実物**(スクショ・コード・テスト結果)で判定する
- 採否は**速く**決める。出揃ったクラスタから即統合に回す(最遅を待たない)
- 不採用は即 `orch clean`。残骸を残さない

## 1. 要件 → タスク分解

要件をクラスタ(独立して統合できる単位)に分け、各項目に対して決める:

| 項目 | 判断基準 |
|---|---|
| コンペ数 n | 機械的/明確な仕様: 1。通常: 3。創造・曖昧・高リスク: 3〜5 |
| モデル | 機械的: mediumのみ。通常: high×1+medium×残り。創造/曖昧/分析: high中心。**maxは boot --allow されたRunでのみ使える**(許可は .orch/state.json に永続化済み。無許可なら orch が機械的に拒否するので、そのまま渡して判定に任せてよい) |
| write_scope | 触ってよい範囲。クラスタ間で非交差にする |
| verify | 機械的に判定できる成功条件(型・テスト・ビルド)を必ず書く |
| deps | 先行タスクの task id。無ければ並列 |

判断の目安(swe2 実測特性由来):
- **medium は曖昧な仕様を確認せず進む傾向** → 仕様が明確な時だけ使う
- **high/max は仕様バグ自体を検出する** → 曖昧・矛盾を含みうる仕事は high 以上
- **max の可否は orch が機械的に判定する**。`orch boot --allow swe-2-max`
  されたRunでは許可が .orch/state.json に永続化されているので、
  環境変数を気にせずそのまま `--model swe-2-max` を渡せばよい
  (バックグラウンドシェルに環境変数が届かない問題は state 永続化で解決済み)
- 同一モデル×同じ入力の複製は誤りが相関する → コンペでは「作らせるもの」を少し変える
  (別アプローチ指定等)か、モデルを混ぜる

## 2. 仕様ファイル

`tasks/` に `<cluster>-<n>.md` で書く。`tasks/template.md` の契約欄は全文残す
(heartbeat/ask/escalation/コミット義務/削除同意は実績上「書かないと守られない」)。

## 3. 投入

```bash
orch run create "<目的>"                      # Run作成(最初の1回)
orch task create tasks/foo.md                 # 単発タスク
orch compete tasks/bar.md -n 3 \
  --models swe-2-high,swe-2-medium,swe-2-medium --cluster bar
orch spawn --task <id> --name w-x --model swe-2-medium   # 個別
orch batch manifest.json                      # 大量投入はこれ(下記)
```

### 大量投入は `orch batch` で(実測由来の必須ルール)

複数タスクの一括投入は JSON マニフェスト + `orch batch` を使う。
**task→name/model の対応を bash の連想配列(`declare -A`)や手書きループで
組まない** — macOS の bash 3.2 には連想配列が無く、マッピングが壊れて
別タスクのspecが混入・連続 spawn 失敗(stale worktree 17個)の実測がある。

```json
{"cluster": "works",
 "defaults": {"model": "swe-2-medium"},
 "items": [
   {"spec_file": "tasks/w01.md", "name": "w01-a", "model": "swe-2-high"},
   {"spec_file": "tasks/w01.md", "name": "w01-b"},
   {"task": "task_abc", "name": "w-retry"}
 ]}
```

`orch batch` は逐次spawn・間隔 pacing(既定8秒)・1件失敗しても継続・
失敗時は worktree/branch/端末を自動ロールバック・`--retries` で再試行する。
結果は `{"spawned": [...], "failed": [...]}` で返り、failed があれば
exit 2 で終わるので補完投入の判断材料にする。

全ての独立タスクを作ってから待機に入る(逐次投入しない)。

## 4. 監視

```bash
orch wait --timeout-min 60            # イベント駆動。worker_done/escalation/question が流れる
orch patrol                           # 全dispatch巡回: 承認メニュー停止・長時間生存を検出
orch patrol --rescue                  # 承認メニューを検出したら自動でキー送信して救出
```

- `orch wait` は `check --wait --types ...` ベースで、処理した
  メッセージを自動 ack する。**sleep ポーリング禁止**
- 生の `orca orchestration check --wait` を直接使う場合、処理した
  メッセージは必ず `orca orchestration ack --id <msg>` する —
  ack しないと同じメッセージが再配信されて何度も処理することになる(実測)
- `question` が来たら `orca orchestration reply --id <msg> --body "<回答>"`
- `escalation` が来たら内容を読んで対処(仕様修正・リトライ・人間に聞く)
- patrol が `zombie?` を報告したら spawn 失敗の残骸 —
  prompt injection が無言で失敗して devin が起動していない実測がある。
  `orch clean --dispatch <id>` で掃除して再投入する
- heartbeat が一定時間無い/端末が沈黙 → patrol で `terminal read` して状態確認
- 15〜60分の無言は正常(コーディングタスクの常態)。timeout≠失敗

## 5. 審査(実物判定)

- 候補の worktree パスは `orch collect` で一覧。ファイルは直接読める
- 「どれが良いか」は demo・スクショ・コード・report.md で**あなたが見て**決める
- 報告の自己評価を鵜呑みにしない
- 候補の個別ブランチを深く監査しない(レビューは統合後の1点に絞る)

## 6. 採否 → 統合(パイプライン)

```bash
orch collect                     # dirty=N が未コミット数。0 以外は採用前に確認
orch adopt --dispatch <採用dispatch>   # 未コミットがあれば warning が出る
orch integrate --topic <cluster> --branches <採用branch,...> \
  --verify "npx tsc --noEmit && npx vitest run"
orch clean --losers          # 決着済みクラスタの敗者 dispatch/worktree/端末を一括削除
```

- **adopt 前に dirty を見る**。worker_done が来ても成果物が未コミットの
  実測がある(採用してもブランチに入らない)。warning が出たら該当
  worktree でコミットさせてから adopt する

- `--losers` は「同クラスタに採用済みがいる未adopt候補」だけを消す。
  生きた integrator/reviewer・採否未確定の候補・採否後に再投入した候補は
  対象外で `kept` として報告される → integrate 直後に回しても安全
- クラスタごと放棄(全滅・やり直し)は `orch clean --cluster <name>`
  (adopted は残る)。`kept` に残ったものを消すなら個別 dispatch 指定
- クラスタ単位で「採否確定→即integrate→即clean losers」。全クラスタを待たない
- インテグレーターは `integ/<topic>` ブランチにコミット。main には触らせない

## 7. レビュー(品質ゲート)

`integ/<topic>` ができたら、別コンテキストの reviewer ワーカーを投げる:
- `orch task create tasks/review-<topic>.md`(仕様: integ ブランチの監査、
  本番ファイルは読むだけ、`reviews/<topic>.md` と `patches/` を自分のブランチにコミット)
- 監査通過をマージ条件にする。指摘の取捨選択はあなたが行う

## 8. 完了

- 全 `integ/` を検証 → main へマージ(マージ許可はユーザー/ゲート次第)
- `orch clean --all` で全 dispatch/worktree/端末を解放
- `orch status` で残骸ゼロを確認
- ユーザーに結果報告: 採用一覧・検証結果・失敗とその理由
