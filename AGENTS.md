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
| medium が曖昧仕様を確認せず実装 | モデル配分表: 曖昧性高い仕事は medium に出さない + テンプレの「迷ったらask必須」 | tasks/template.md, playbook |

## 使い方

```bash
orch init                          # repo登録 + .orch/ 準備(対象repoのルートで)
orch run create "<目的>"           # Run作成
orch compete tasks/x.md -n 3 \
  --models swe-2-high,swe-2-medium,swe-2-medium
orch wait --timeout-min 60         # イベント駆動で完了検知
orch patrol [--rescue]             # 停止検出・救出
orch collect                       # 完了候補の branch/worktree 一覧
orch adopt --dispatch <id>
orch integrate --topic ui --branches b1,b2 --verify "npx tsc --noEmit"
orch clean --losers                # 不採用を一括削除
orch status
```

全自動モード: `orch boot requirement.md` がコーディネーター端末
(swe-2-high, bypass)を立てて playbook を実行させる。
`--allow swe-2-max` を付けると coordinator 自体が swe-2-max で起動し、
`ORCH_ALLOW=swe-2-max` が環境変数に設定されてワーカー/インテグレーターにも
max を配分できるようになる(ORCH_ALLOW 無しでの max 指定は orch が機械的に拒否)。
人間は要件投入と最終マージ承認だけ。

## モデル配分(実測特性より)

| 層 | モデル | 根拠 |
|---|---|---|
| コーディネーター | swe-2-high。`--allow swe-2-max` なら max | 審査・採否・統合判断が主業務 |
| max全般 | `orch boot --allow swe-2-max` のRunのみ | `ORCH_ALLOW` 無しでの max 指定は spawn/compete/integrate が機械的に die |
| インテグレーター | swe-2-high | マージ方針の判断が成果物に直結 |
| 候補ワーカー | 難易度で変える: 機械的=medium, 通常=high×1+medium, 創造/曖昧=high以上 | medium は曖昧仕様で確認せず進む実測あり |
| レビュー/検証 | swe-2-medium〜high | チェックリスト型監査 |

## やってはいけないこと

- 素の `devin`(bypass無し)で無人ワーカーを起動しない — `orch spawn` を必ず使う
- sleep で完了を待たない — `orch wait` / `wait-for '<条件>'` を使う
- 候補worktreeを直接編集しない(読むのは可)。本番への書き込みは integrator 経由
- 採否確定後に不採用worktreeを放置しない — `orch clean` を機械的に回す
- sessions.db / トランスクリプトの中身をrepoにコミットしない(個人情報の塊)
