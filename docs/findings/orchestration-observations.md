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
