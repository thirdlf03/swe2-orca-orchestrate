# swe2-orca-orchestrate

Orca + 任意の AI コーディングエージェント(Claude Code / Codex / OpenCode /
Devin …)向けのマルチエージェント・オーケストレーションハーネス。
Devin CLI (SWE-2) 専用として始まり、1ヶ月の実測
([docs/findings/devin-month-report.md](docs/findings/devin-month-report.md))
をもとに汎用化した。

「エージェント数ではなく、独立して検証可能な仕事の単位を増やす」が設計思想。
計測済みの失敗パターンはルールではなく `bin/orch`・テンプレート・hooks に
機械的に埋め込む(自然言語ルールに強制力はない — 月次レポートでも再確認)。
エージェント固有の知識(起動 argv・バイパスフラグ・画面シグネチャ・tier→モデル・
ログの場所)は `adapters/<id>.json` のデータにする。詳細は
[docs/adapters.md](docs/adapters.md)。

## 構成

```
bin/orch           ハーネスCLI(spawn/batch/wait/patrol/collect/adopt/integrate/clean/boot/agents/hooks)
bin/wait-for       条件待ちプリミティブ(sleep乱打の代替)
adapters/*.json    エージェント定義(devin/claude/codex/opencode/generic)
lib/orchlib/       adapters(解決・gate)/ events(.orch/events.jsonl)/ hooks / metrics
tasks/template.md      候補ワーカー仕様テンプレ(ライフサイクル義務を埋め込み済み)
tasks/integrator.template.md  インテグレーター仕様テンプレ
playbooks/coordinator.md  コーディネーターの行動規範(全自動時はこれを実行する端末を立てる)
spec/run.example.json   オーケストレーション仕様IR(DAG・エージェント配分・ゲート)
hooks/no_blind_sleep.py 無条件sleepをブロックするPreToolUseフック(全エージェント共通ポリシー)
hooks/opencode/         opencode 用プラグインのテンプレ
docs/findings/          実測分析(月次レポート・sessions.db 追加分析)
```

## アーキテクチャ

```
人間 ──要件──> コーディネーター (任意アダプタの high tier。`--allow <agent>:max` なら max)
                  │ 分解・仕様化・採否・マージ判断(判断はここだけ)
                  ├─ 候補ワーカー群 (worktree分離・n案コンペ・エージェント/tier混合)
                  ├─ インテグレーター (採用ブランチ→integ/<topic>へ統合)
                  ├─ レビューワーカー (integブランチ監査・パッチ候補)
                  └─ orch patrol (承認停止・起動ゲート・レート制限・沈黙の検出と救出)
```

Orca の思想に合わせ、**スケジューラは作らない**。Run は名前空間+inbox、
配置・並列度の判断はコーディネーターエージェントが行う。
モデル指定子は全コマンド共通で `[<agent>:]<tier|model-id>`(tier = medium/high/max)。
既定エージェントは `--agent` > `ORCH_AGENT` > run state(`orch boot --agent`)> devin。

## 計測済み失敗 → 機械的対策

| 失敗(実測) | 対策 | 対象 | 場所 |
|---|---|---|---|
| 承認メニュー無言停止(5端末×15-30分) | `orch spawn` がアダプタの bypass フラグを必ず付けて起動。残存は patrol が検出→`--rescue` で `approval_answer` の選択肢を押す | 汎用 | bin/orch, adapters |
| プロンプトが入力欄に残りEnter未送信 | 送信後にアダプタの `input_echo` で「入力欄から消えたか」を検証しリトライ、`[Pasted text` 残留は Enter で救出 | 汎用 | bin/orch |
| 準備完了判定が `SWE-2 (Medium\|High\|Max)` 文字列依存・`terminal wait --for tui-idle` が devin を認識しない | readiness をアダプタの strategy(screen-regex / orca-tui-idle / both)に移した。devin は screen-regex | Devin固有 | adapters/devin.json |
| 起動時の workspace trust ゲートで初手から停止(Claude で実測) | アダプタの `startup_prompts` を準備待ち中に自動応答。起動後に出たものは patrol `--rescue` が押す | 汎用 | bin/orch, adapters |
| sleepポーリングで2h空費 | `orch wait` = `check --wait --types worker_done,escalation,question`+ackチェーン。heartbeat滞留はtypesフィルタで回避 | 汎用 | bin/orch |
| heartbeat/ask 未使用(heartbeat 0回が 22-37%) | 仕様テンプレに義務として全文埋め込み。死活は patrol がハーネス側で判定し heartbeat は補助扱い | 汎用 | tasks/template.md, bin/orch |
| 共有アカウントの rate limit で一斉停止(30並列全滅・56プロセス・人手再開95件) | provider 単位の並列 cap(既定10・`--cap anthropic=6,openai=4`)。patrol が rate_limit 画面を検出→その provider の cap を半減して state 永続化(`caps_auto` — env 指定の cap より優先される上限)、`--rescue` で cooldown 後に `resume_text` を送る | 汎用 | bin/orch |
| 統合が最遅ワーカー待ち(TTD p90/p50=3.7) | playbook の「採否確定→即integrate」+ `orch batch --sort-weight`(LPT) | 汎用 | playbook, bin/orch |
| 不採用残骸(170ファイル/3.1万行) | `orch clean --losers` で dispatch解放+worktree+端末を一括削除 | 汎用 | bin/orch |
| `clean --losers` が生きた integrator を kill | loser を「同クラスタに採用済み兄弟を持つ未adopt候補」と再定義。他は kept として報告。クラスタ放棄は `--cluster` | 汎用 | bin/orch |
| medium が曖昧仕様を確認せず実装 | 配分表: 曖昧な仕事は medium tier に出さない + テンプレの「迷ったらask必須」+ Jev gate | 汎用 | tasks/template.md, playbook |
| `declare -A` が bash 3.2 で不発 → spec混入+連続spawn失敗 | 大量投入は `orch batch <manifest.json>`。逐次spawn・pacing・per-item retry・1件失敗でも継続 | 汎用 | bin/orch, playbook |
| `worker-start failed` で worktree/branch/端末が残骸化 | spawn 失敗時は確保したリソースを全てロールバック | 汎用 | bin/orch |
| coordinator が生の `worker-start` を叩く(P2 でも25件) | playbook・boot prompt・エラー文で `orch spawn/batch/compete` に誘導(Orca 側なので機械的抑止は不可) | 汎用 | playbook, bin/orch |
| `ORCH_ALLOW` が background shell に届かず max spawn が途中から失敗 | `--allow` を run の .orch/state.json に永続化。env→stateの順で判定 | 汎用 | bin/orch |
| worker_done 後に成果物が未コミット | `orch collect` が dirty 数を表示、`orch adopt` が未コミットを warning | 汎用 | bin/orch |
| zombie dispatch が status 上は生きて見える | patrol が Orca の `agentWait` → 画面 regex → 経過時間の順に判定し、未開始stateの滞留を `zombie?` と報告 | 汎用 | bin/orch |
| 未ackメッセージが再配信され処理がループ | `orch wait` は処理メッセージを自動ack。playbookに生 check 使用時の ack 義務を明記 | 汎用 | bin/orch, playbook |
| ワーカーが hook に繰り返しブロックされ代替を再学習(注意書き追加後も拒否率 3.8%→4.3%) | 代替手段はエージェント依存なので、アダプタの `spec_notes` を spec 末尾に自動注入(compete/batch/integrate) | 汎用 | bin/orch, adapters |
| hook の配布がエージェントごとに違う | ポリシーは `no_blind_sleep.py` 1本。claude は起動ごとに `--settings` で注入(グローバル設定に触らない)、他は `orch hooks install --agent X --apply` | 汎用 | lib/orchlib/hooks.py |
| 重さ・所要時間の計測が Devin `sessions.db` 依存 | orch 自身が `.orch/events.jsonl`(spawn/done/adopt/clean/rate_limit/cap_lowered/patrol)を書く。トークンはアダプタの transcript reader で補完 | 汎用 | lib/orchlib/events.py, metrics.py |
| 曖昧spec・配分ミスの事前判断材料がない | spawn時に (agent,model,spec長) 中央値の予測を `.orch/estimates.jsonl` に記録。`orch weights`/`orch estimate` | 汎用 | bin/orch |
| orch 失敗の 1/3 が usage エラー | 未知サブコマンドに did-you-mean と一覧を返す。各コマンドは引数不足で usage 例を出す | 汎用 | bin/orch |
| coordinator が全候補を実物審査(1Runで eval 328回) | `orch score` — Jev で候補を投機的採点し上位だけ審査(advisory) | 汎用 | bin/orch |
| devin の `--model` 部分一致で max が gate をすり抜ける | `fuzzy: true` のアダプタは `max` を含む/gated id の接頭辞も max 扱い | Devin固有 | adapters/devin.json |

## 使い方

```bash
orch init                          # repo登録 + .orch/ 準備(対象repoのルートで)
orch agents                        # アダプタ一覧: CLIの有無・tier→モデル・生存数・未検証項目
orch agents --check claude:high    # spawn が実際に打つコマンドを表示(端末は作らない)
orch hooks install --agent devin --apply   # 永続 hook 導入(省略時 dry run。claude は不要)
orch run create "<目的>"           # Run作成
orch batch manifest.json           # 大量投入(task/name/model対応をJSONで)
orch compete tasks/x.md -n 3 \
  --models claude:high,codex:medium,devin:medium   # エージェント混合コンペ
orch wait --timeout-min 60         # イベント駆動で完了検知
orch patrol [--rescue] [--jev]     # 承認停止・起動ゲート・rate limit の検出と救出
orch collect                       # 完了候補の branch/worktree/dirty 一覧
orch score [--cluster c]           # 実験: Jev で候補を事前採点(要 TYPESAFE_API_KEY)
orch adopt --dispatch <id>
orch integrate --topic ui --branches b1,b2 --verify "npx tsc --noEmit"
orch clean --losers                # 決着済みクラスタの敗者のみ削除
orch clean --cluster <name>        # クラスタごと放棄(adoptedは残る)
orch status                        # provider ごとの生存数と cap も出る
orch weights / orch estimate m.json / orch batch m.json --sort-weight
```

全自動モード: `orch boot requirement.md --agent claude` がコーディネーター端末
(そのエージェントの high tier, bypass)を立てて playbook を実行させる。
`--agent` は run の state.json に永続化され、ワーカーの既定エージェントにもなる。
- `--allow claude:max`(または `--allow claude`)で coordinator 自体も max になり、
  ワーカー/インテグレーターにも claude の max を配分できる。`--allow max` は全エージェント、
  `--allow swe-2-max` は後方互換で `devin:max`。許可は state.json に永続化される
  (無許可での max 指定は orch が機械的に拒否。ORCH_ALLOW env は補助経路)
- `--cap N` は全 provider 共通、`--cap anthropic=6,openai=4` は provider 別の並列上限
  (既定10・0で無効・state 永続化)。env `ORCH_PARALLEL_CAP_<PROVIDER>` /
  `ORCH_PARALLEL_CAP` が優先。カウントは「直近 `ORCH_PARALLEL_IDLE_MIN` 分(既定15)に
  活動したセッション」— 残置セッションは枠を食わない
- `--jev` でこのRun全体の Jev 連携を有効化(下記)

人間は要件投入と最終マージ承認だけ。実起動スモークの手順は
[docs/adapters.md](docs/adapters.md#未検証チェックリスト)。

## 実験: Jev 連携(意味判定の安い層)

`orch boot --jev` で Run 単位に有効化するのが正規の使い方
(個別コマンドの `--jev` フラグ or `ORCH_JEV` env でも有効化できる)。
全経路とも `TYPESAFE_API_KEY` が必要で、キー無し・API障害時は全て非Jev経路に
フォールバックする。差分/report.md が外部APIに送られる点だけ注意。

| 場所 | 動作 |
|---|---|
| `orch boot --jev` | Run 全体で Jev 有効化。coordinator prompt に利用方法を注入 |
| `orch score` | 候補を spec 適合度(noul)+採用価値(score)でランキング。advisory |
| `spawn/compete/batch` | spec 曖昧性を採点。曖昧(p>=0.7)× medium tier(全エージェント)は機械的に die、他 tier は警告 |
| `patrol` | screen tail を読んで「停止/待機中か」を判定 → `jev-stuck:p=` を issues に追加 |
| `adopt` | 完了主張の妥当性を採点。低確率なら warning |

採否の最終決定には使わない。誤ブロックが多ければ `JEV_AMBIGUITY_BLOCK`(0.7) を上げる。

## モデル配分(tier で書く)

| 層 | tier | 根拠 |
|---|---|---|
| コーディネーター | high。`--allow <agent>:max` なら max | 審査・採否・統合判断が主業務 |
| max全般 | `orch boot --allow ...` のRunのみ | 無許可での max は spawn/compete/batch/integrate が機械的に die |
| インテグレーター | high | マージ方針の判断が成果物に直結 |
| 候補ワーカー | 機械的=medium, 通常=high×1+medium, 創造/曖昧=high以上 | medium は曖昧仕様で確認せず進む実測あり |
| レビュー/検証 | medium〜high | チェックリスト型監査 |

tier→実モデルの対応はアダプタごと(例: claude high=opus、codex medium=gpt-5.6-terra、
devin high=swe-2-high)。`orch agents` で確認する。
モデル id は頻繁に入れ替わるので、アダプタを書く・直すときは必ず各 CLI の最新一覧や
公式ドキュメントを参照する(手順は [docs/adapters.md](docs/adapters.md) の
「モデル id は最新を参照する」)。`orch agents` は一覧から消えた tier モデルを
`stale_models` として報告する。provider を混ぜると
rate limit が分散する(cap は provider 単位)。

## やってはいけないこと

- エージェントを bypass 無しで無人ワーカーとして起動しない — `orch spawn` 系以外で起動しない
- 生の `orca orchestration worker-start` を呼ばない — `orch spawn/batch/compete/integrate` を使う
  (ロールバック・cap・gate・spec_notes 注入・イベント記録が全て抜ける)
- 複数ワーカーの投入を手書きシェルループ(特に `declare -A`)で組まない — `orch batch`
- sleep で完了を待たない — `orch wait` / `wait-for '<条件>'` を使う
- 候補worktreeを直接編集しない(読むのは可)。本番への書き込みは integrator 経由
- 採否確定後に不採用worktreeを放置しない — `orch clean` を機械的に回す
  (「消えない残骸」は kept 出力を見て `--cluster` か個別指定)
- sessions.db / トランスクリプトの中身をrepoにコミットしない(個人情報の塊)
