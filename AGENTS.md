# swe2-orca-orchestrate

Orca + Devin CLI (SWE-2) を無制限に使う前提のマルチエージェント・
オーケストレーションハーネス。

「エージェント数ではなく、独立して検証可能な仕事の単位を増やす」が設計思想。
計測済みの失敗パターンはルールではなく `bin/orch`・テンプレート・hooks に
機械的に埋め込む(自然言語ルールに強制力はない — swe2-optimize の実測より)。

## 構成

```
bin/orch           ハーネスCLI(spawn/wait/patrol/collect/adopt/integrate/clean/boot)
bin/wait-for       条件待ちプリミティブ(sleep乱打の代替)
tasks/template.md      候補ワーカー仕様テンプレ(ライフサイクル義務を埋め込み済み)
tasks/integrator.template.md  インテグレーター仕様テンプレ
playbooks/coordinator.md  コーディネーターの行動規範(全自動時はこれを実行する端末を立てる)
spec/run.example.json   オーケストレーション仕様IR(DAG・モデル配分・ゲート)
hooks/no_blind_sleep.py 無条件sleepをブロックするPreToolUseフック
docs/findings/          sessions.db 追加分析
```

## アーキテクチャ

```
人間 ──要件──> コーディネーター (default swe-2-high。`--allow swe-2-max` で coordinator 自体も max)
                  │ 分解・仕様化・採否・マージ判断(判断はここだけ)
                  ├─ 候補ワーカー群 (worktree分離・n案コンペ・モデル混合)
                  ├─ インテグレーター (採用ブランチ→integ/<topic>へ統合)
                  ├─ レビューワーカー (integブランチ監査・パッチ候補)
                  └─ orch patrol (承認停止・沈黙の検出と救出)
```

Orca の思想に合わせ、**スケジューラは作らない**。Run は名前空間+inbox、
配置・並列度の判断はコーディネーターエージェントが行う。

## 計測済み失敗 → 機械的対策

| 失敗(実測) | 対策 | 場所 |
|---|---|---|
| 承認メニュー無言停止(5端末×15-30分) | `orch spawn` が必ず `--permission-mode bypass` で起動。残存は patrol が検出→`--rescue` で救出 | bin/orch |
| プロンプトが入力欄に残りEnter未送信(実測: boot直後にcoordinator無言) | `_send_prompt_devin` で送信後に「入力欄から消えたか」を検証しリトライ。`terminal wait --for tui-idle` は devin を既知agentと認識せず早期returnするため使わない | bin/orch |
| sleepポーリングで2h空費 | `orch wait` = `check --wait --types worker_done,escalation,question`+ackチェーン。heartbeat滞留はtypesフィルタで回避 | bin/orch |
| heartbeat/ask 未使用(実測 hb計28回/ask 0) | 仕様テンプレに義務として全文埋め込み(プリアンブルは守られない実績) | tasks/template.md |
| 統合が最遅ワーカー待ち | playbook の「採否確定→即integrate」。クラスタ単位でパイプライン | playbooks/coordinator.md |
| 不採用残骸(170ファイル/3.1万行) | `orch clean --losers` で dispatch解放+worktree+端末を一括削除 | bin/orch |
| `clean --losers` が spawn 直後の生きた integrator を kill(実測) | loser を「同クラスタに採用済み兄弟を持つ未adopt候補」と再定義。integrator/reviewer/採否未確定・再投入候補はマッチせず kept として報告。クラスタ放棄は `--cluster` | bin/orch |
| medium が曖昧仕様を確認せず実装 | モデル配分表: 曖昧性高い仕事は medium に出さない + テンプレの「迷ったらask必須」 | tasks/template.md, playbook |
| coordinator の `declare -A` が bash 3.2 で不発 → 別spec混入+連続spawn失敗(stale worktree 17個) | 大量投入は `orch batch <manifest.json>`(task/name/model対応をJSONで記述)。逐次spawn・間隔pacing・per-item retry・1件失敗でも継続 | bin/orch, playbook |
| `worker-start failed` で worktree/branch/端末が残骸化(17 worktrees/62 branches) | spawn 失敗時は確保したリソースを全てロールバック | bin/orch |
| `ORCH_ALLOW` がcoordinator生成のbackground shellに届かず max spawn が途中から失敗 | `--allow` を run の .orch/state.json に永続化。env→stateの順で判定 | bin/orch |
| worker_done 後に成果物が未コミット(2件実測) | `orch collect` が dirty 数を表示、`orch adopt` が未コミットを warning | bin/orch, playbook |
| spawn されたが devin が起動していない zombie dispatch が status 上は生きて見える(数時間検出不能) | `orch patrol` が未開始stateの滞留を `zombie?` として報告 | bin/orch |
| 未ackメッセージが `check --wait` で再配信され処理がループ | `orch wait` は処理メッセージを自動ack。playbookに生 check 使用時の ack 義務を明記 | bin/orch, playbook |
| ワーカーが毎回 hook に複数回ブロックされ代替を再学習(23hで308ステップ) | hook 遵守・代替手段を仕様テンプレに定型句として注入 | tasks/template.md |
| coordinator が全候補を実物審査(1Runで eval 328回・screenshot 112回) | `orch score` — Jev(TypeSafe System One)で候補を投機的採点し上位だけ審査。採否自体は coordinator の判断のまま(advisory) | bin/orch |

## 使い方

```bash
orch init                          # repo登録 + .orch/ 準備(対象repoのルートで)
orch run create "<目的>"           # Run作成
orch batch manifest.json           # 大量投入(JSONでtask/name/model対応。逐次+pacing+rollback)
orch compete tasks/x.md -n 3 \
  --models swe-2-high,swe-2-medium,swe-2-medium
orch wait --timeout-min 60         # イベント駆動で完了検知
orch patrol [--rescue] [--jev]     # 停止検出・救出(--jev で画面の意味判定)
orch collect                       # 完了候補の branch/worktree 一覧
orch score [--cluster c]           # 実験: Jev で候補を事前採点(要 TYPESAFE_API_KEY)
orch adopt --dispatch <id>
orch integrate --topic ui --branches b1,b2 --verify "npx tsc --noEmit"
orch clean --losers                # 採否確定クラスタの敗者のみ削除(後続dispatchは巻き込まない)
orch clean --cluster <name>        # クラスタごと放棄(adoptedは残る)
orch status
```

全自動モード: `orch boot requirement.md` がコーディネーター端末
(swe-2-high, bypass)を立てて playbook を実行させる。
`--allow swe-2-max` を付けると coordinator 自体が swe-2-max で起動し、
許可が run の .orch/state.json に永続化されてワーカー/インテグレーターにも
max を配分できるようになる(許可無しでの max 指定は orch が機械的に拒否。
ORCH_ALLOW 環境変数は後方互換の補助経路 — background shell には届かない
実測があるため state 永続化が本経路)。
`--jev` を付けるとこのRun全体で Jev 連携が有効化され(下記「実験」節)、
coordinator の prompt に使い方が注入される。
人間は要件投入と最終マージ承認だけ。

## 実験: Jev 連携(意味判定の安い層)

`orch boot --jev` で Run 単位に有効化するのが正規の使い方
(個別コマンドの `--jev` フラグ or `ORCH_JEV` env でも有効化できる)。
有効化の判定は 3経路 — フラグ / env / run の `.orch/state.json` 永続化。
ORCH_ALLOW と同じ教訓で state が本経路。全経路とも `TYPESAFE_API_KEY` が
必要で、キー無し・API障害時は全て非Jev経路にフォールバックする
(early-access API を単一障害点にしない)。差分/report.md が外部APIに
送られる点だけ注意。

TypeSafe Jev(System One 決定モデル)で「機械判定」と「コーディネーター審査」の
間の意味判定を肩代わりする。

| 場所 | 動作 |
|---|---|
| `orch boot --jev` | Run 全体で Jev 有効化。coordinator prompt に利用方法を注入 |
| `orch score` | 候補を spec 適合度(noul)+採用価値(score)でランキング。advisory |
| `spawn/compete/batch` | spec 曖昧性を採点(vague/divergent の max)。曖昧(p>=0.7)×medium は機械的に die、他層は警告 |
| `patrol` | screen tail を読んで「停止/待機中か」を判定 → `jev-stuck:p=` を issues に追加 |
| `adopt` | 完了主張の妥当性を採点。低確率なら warning(dirty warning と併記) |

採否の最終決定には使わない — 「判断はコーディネーターの仕事」の原則を
壊さない範囲で、候補の絞り込みと警告に留める。
実API校正済み: 空spec p=0.94ブロック / ルール未定spec 0.73ブロック /
機械リネームspec <0.4 スルー。誤ブロックが多ければ
`JEV_AMBIGUITY_BLOCK`(0.7) を上げるのが調整ポイント。

## モデル配分(実測特性より)

| 層 | モデル | 根拠 |
|---|---|---|
| コーディネーター | swe-2-high。`--allow swe-2-max` なら max | 審査・採否・統合判断が主業務 |
| max全般 | `orch boot --allow swe-2-max` のRunのみ | 許可は state.json に永続化。無許可での max 指定は spawn/compete/batch/integrate が機械的に die |
| インテグレーター | swe-2-high | マージ方針の判断が成果物に直結 |
| 候補ワーカー | 難易度で変える: 機械的=medium, 通常=high×1+medium, 創造/曖昧=high以上 | medium は曖昧仕様で確認せず進む実測あり |
| レビュー/検証 | swe-2-medium〜high | チェックリスト型監査 |

## やってはいけないこと

- 素の `devin`(bypass無し)で無人ワーカーを起動しない — `orch spawn` を必ず使う
- 複数ワーカーの投入を手書きシェルループ(特に `declare -A`)で組まない —
  `orch batch` を使う。bash 3.2 には連想配列がなく、対応表が壊れて
  spec混入+spawn storm の実測がある
- sleep で完了を待たない — `orch wait` / `wait-for '<条件>'` を使う
- 候補worktreeを直接編集しない(読むのは可)。本番への書き込みは integrator 経由
- 採否確定後に不採用worktreeを放置しない — `orch clean` を機械的に回す
  (`--losers` は決着済みクラスタの敗者だけを選ぶので、integrate 直後に
  回しても生きた integrator は殺さない。安全側に倒した設計なので
  「消えない残骸」があれば kept 出力を見て --cluster か個別指定を使う)
- sessions.db / トランスクリプトの中身をrepoにコミットしない(個人情報の塊)
