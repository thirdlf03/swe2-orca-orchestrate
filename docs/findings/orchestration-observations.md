# オーケストレーション観測メモ — sessions.db 追加分析 + 実機スモーク

`sessions.db` 2件(本PC 95セッション / 別環境のハッカソンDB 195セッション・167ワーカー)
を対象に、ライフサイクル遵守とコマンド使用を集計。加えてハーネスの実機スモークで
見つかった新規の失敗モードを記録する。

## 定量(両DB)

| 指標 | 本PC (95s) | ハッカソンDB (195s/167w) |
|---|---|---|
| worker_done 送信 | 15/18 workers | 164/167 workers (98%) |
| heartbeat 0回のワーカー | — | **48/167 (29%)** |
| ask 使用 | 1 | 8 workers・計14回 |
| escalation 使用 | 0 | **0** (全DBで未使用) |
| sleep呼出(ツール引数) | 316回・6.8h | 439回・5.9h |
| `check --types` 使用率 | 7/9 | 51/52 |

観測:
- worker_done の到達率は高い(98%)が、**heartbeat は3割のワーカーが一度も送らない**
  → 「死活監視したいなら heartbeat 任せにせず、コーディネーター側の patrol で
  端末を覗く」が正しい設計(このハーネスの `orch patrol`)
- **escalation は誰も使わない**。ask も稀。→ ハング検知は「メッセージ待ち」では
  なく「画面の停止検知」でやる必要がある
- worker側 sleep の大半は仕様通りの heartbeat ループ(`sleep 300`)であり合法。
  悪いのはコーディネーター側の完了待ち sleep(本PCで `check --wait` が9回のみ)

## 新規の失敗モード(スモークテストで発見・修正済み)

**`agent_prompt_stalled`**: `worker-start --terminal <devin端末>` が devin TUI
起動完了前に仕様を注入すると、プロンプトが `[Pasted text #N +130 lines]` として
入力欄に残り Enter が送られず dispatch_input で失敗する。
`terminal wait --for tui-idle` は devin(known-agent ではない)に対して
起動完了を待てない。

対策(実装済み・実機確認): `orch spawn` は `terminal read --screen` を
`SWE-2 (Medium|High|Max)` ステータスバー出現までポーリングしてから
worker-start する。stalled 時は `[Pasted text` を検出して Enter を送る救出を行う。
- 救出された dispatch は capability が revoke されるが、worker_done メッセージ自体は
  Run inbox に届く(実測)。ただし正式な成立扱いにならない可能性があるため、
  ** readiness を先に確保するのが本筋**

## ハッカソンDB由来の既知パターン(再確認)

相手方 AGENTS.md の改善優先度と整合:
- 承認プロンプト停止 → spawn 時の `--permission-mode bypass` 強制で防ぐ
- `check --wait` の heartbeat 滞留 → `--types` フィルタが必要(本DBでも 51/52 が使用)
- 残骸削除 → `orch clean --losers` で機械化

## 大規模本番Run(2026-09-17・16h・ワーカー20+審査系)での追加失敗モード

swe2-optimize 側の sessions.db 全件分析(186セッション・15,715 tool calls)で
定量確認された失敗と、実装した対策の対応表。詳細な計測値は
swe2-optimize `docs/tool-effectiveness.md` 参照。

| 失敗(実測) | 対策(実装済み) |
|---|---|
| coordinator の `declare -A`(bash 3.2に無い)で task→model 対応が壊れ、別spec混入+連続spawn失敗 | `orch batch manifest.json` — 対応表はJSONデータ。逐次spawn・pacing(既定8s)・per-item retry・1件失敗でも継続(exit 2) |
| `worker-start failed: null` 連発で stale worktree 17個/branch 62本 | `_spawn_one` が失敗時に terminal close→worktree rm→branch -D を全ロールバック。エラーに screen tail を添付して `null` だけの無情報を解消 |
| `ORCH_ALLOW` がcoordinator生成のbackground shellに届かず max spawn が途中から失敗 | `orch boot --allow` を run の `.orch/state.json` に永続化。env→stateの順で判定するので shell 由来の env 喪失に耐える |
| 未ackメッセージが `check --wait` で再配信され coordinator が同じメッセージを繰り返し処理 | `orch wait` は従来通り deliveryId を次回 `--ack` に連鎖。playbook に生 `check --wait` 使用時の ack 義務を明記 |
| worker_done 済みだが成果物未コミット(2件) | `orch collect` に dirty 数を表示、`orch adopt` が未コミットを warning(ブロックはしない — 採否は coordinator の判断) |
| spawn されたが devin が起動していない zombie dispatch が `orch status` 上は生きて見える(数時間検出不能) | `orch patrol` が未開始 state(dispatched/pending/created/starting/accepted/queued)が --stale-min を超えたものを `zombie?` として報告 |
| ワーカーが毎回 fileop/sleep hook に複数回ブロックされて代替を再学習(23hで308ステップ) | worker/integrator 両テンプレに「hookにブロックされたら別手段で再試行しない・専用ツールとworkdirを使う」を定型句として注入 |
| coordinator が意図せず swe-2-max で起動(配分ガイドライン違反) | coordinator は既定 swe-2-high。max は `orch boot --allow` 明示時のみ(従来通り・維持) |

### 残課題・限界

- **fork したセッションは親作成時点の hook セットを継承する**(Devin CLI 側の仕様と推定)。
  親が fileop hook 登録前に作られた fork では `cd &&` 系が素通り実行された。
  orch 側では制御不能 — hook 更新直後の fork は旧ルールが残る可能性として記録。
- `worker-start failed: null` の根本原因(orca 側の null エラー)は未特定。
  rollback で残骸化は防いだが、連続 spawn が失敗しやすい仮説(rate limit?)は
  pacing で緩和しているだけで、原因自体の修正は orca 側の問題。
- `zombie?` 検出は worker-show の state 名に依存する。orca が新しい
  state 名を返す場合は NEVER_STARTED_STATES への追加が必要。
