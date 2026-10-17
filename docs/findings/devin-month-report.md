# Devin CLI 1ヶ月利用ログ分析 — 汎用ハーネス化に向けて

対象期間: 2026-09-15 〜 2026-10-14(JST)。Devin `sessions.db` 492セッション /
tool call 56,415件 / message_nodes 167,688件、Orca `orchestration.db`
(Run 43・task 455・dispatch 450・message 1,061)、Devin CLI ログ 1,226ファイル。
全て read-only で集計。本書は集計値のみを載せる(プロンプト本文・ファイル内容・
ホーム配下のパスは含めない。repo は basename)。

目的: このハーネス(Devin 専用)を **Orca + 任意の AI コーディングエージェント**
(Claude Code / Codex / OpenCode / Devin …)向けの汎用ハーネスにするために、
観測された問題を **(a) エージェント非依存の汎用問題** と
**(b) Devin / SWE-2 固有の問題** に切り分ける。

## 0. 方法と注意点

- ロール推定: 初回プロンプトの dispatch プリアンブル内 task ID で
  `orchestration.db tasks` と結合(421/450 dispatch が結合)。task のタイトル/spec
  冒頭から candidate / integrator / reviewer に分類。非ワーカーのうち
  dispatch 系コマンド(`worker-start` / `task-create` / `orch spawn|batch|compete|boot|integrate`)
  を発行したセッションを coordinator、残りを ad-hoc とした(ヒューリスティック)。
- `message_nodes.created_at` はバッチ保存時刻で、セッション内の経過時間には使えない
  (span 中央値 0分)。所要時間は Orca の `dispatched_at → completed_at` を使った。
- `tool_call_state` にはタイムスタンプが無いので、tool call の時期は所属セッションの
  作成時刻で代用した。
- フェーズ区分(git log 準拠):
  - **P0**: 〜9/17 00:08。ハーネス未使用(素の orca orchestration)
  - **P1**: 9/17 〜 9/21 02:39。orch 初版 → hardening(rollback/batch/allow 永続化/hook定型句)・Jev
  - **P2**: 9/21 02:39 〜。並列 cap + 重さ推定
- **交絡に注意**: セッションの 74% が初週(9/14週)に集中している。P2 は母数が小さく、
  並列度も低い(最大9)。前後比較は「傾向」として読むこと。

## 1. 量(Volume)

### 週 × ロール(セッション数)

| 週(月曜) | 計 | coordinator | candidate | integrator | reviewer | ad-hoc |
|---|---|---|---|---|---|---|
| 09/14 | 363 | 18 | 225 | 50 | 37 | 33 |
| 09/21 | 90 | 7 | 49 | 11 | 19 | 4 |
| 09/28 | 22 | 2 | 14 | 2 | 0 | 4 |
| 10/05 | 16 | 0 | 13 | 0 | 0 | 3 |
| 10/12 | 1 | 0 | 0 | 0 | 0 | 1 |
| **計** | **492** | **27** | **301** | **63** | **56** | **45** |

- 稼働日 17日。1日あたり中央値 13・最大 139(9/16)。上位日: 9/16=139, 9/17=98, 9/18=62, 9/15=56, 9/21=44
- 91% がワーカー(420/492)。382 セッションが Orca worktree 内で実行された
- coordinator 1 に対しワーカー約 15.6

### モデル × ロール

| モデル | coord | candidate | integrator | reviewer | ad-hoc | 計 |
|---|---|---|---|---|---|---|
| swe-2-high | 24 | 180 | 63 | 23 | 35 | 325 |
| swe-2-medium | 2 | 107 | 0 | 29 | 5 | 143 |
| swe-2-max | 1 | 2 | 0 | 1 | 4 | 8 |
| (未記録) | 0 | 12 | 0 | 3 | 1 | 16 |

- 配分表どおり integrator は全て high。medium は 9/28 週以降ほぼ消えた(2件)

### repo(basename)

vibey-clover 298 / hackz-blitz 93 / ac_krz_three 22 / hakoniwa 17 / ac_krz 16 /
wine-steamvr 14 / perugraph 12 / その他 20。上位2 repo で 79%。

### トークンと生成時間(ロール別)

| ロール | n | 入力tok計 | 出力tok計 | 生成時間 中央値 / p90 (分) | ターン中央値 |
|---|---|---|---|---|---|
| coordinator | 27 | 81.5M | 7.2M | 101 / 272 | 488 |
| candidate | 301 | 197.0M | 34.9M | 36 / 108 | 110 |
| integrator | 63 | 37.4M | 5.2M | 17 / 65 | 77 |
| reviewer | 56 | 45.7M | 5.8M | 25 / 96 | 91 |
| ad-hoc | 45 | 68.3M | 6.1M | 31 / 141 | 102 |

coordinator は全体の 5% のセッションで入力トークンの 19% を消費(長寿命・高ターン)。

## 2. ツール利用と失敗

### ツール別(全 56k 件)

| ツール | 件数 | 比率 | failed | 備考 |
|---|---|---|---|---|
| exec | 22,383 | 39.7% | 1,664 (7.4%) | うち hook 拒否 1,657 / exit≠0 1,271 (5.7%) / exit未記録 2,759 |
| read | 14,134 | 25.1% | 32 | |
| edit | 9,644 | 17.1% | 101 (1.0%) | 89 は old_string 不一致 |
| grep | 3,207 | 5.7% | 40 | worktree 削除後のパス参照など |
| get_output | 2,998 | 5.3% | 27 | バックグラウンド shell のポーリング |
| write | 2,483 | 4.4% | 1 | |
| mcp_call_tool | 169 | 0.3% | 48 (28%) | 外部 MCP 側エラー |
| webfetch | 63 | 0.1% | 10 | 404 |

- ロール別 exec 比率: coordinator 58% (+get_output 14%)、candidate 33%、integrator 46%
  → coordinator は「シェルで他エージェントを操作する」仕事がほとんど
- get_output は coordinator 1,187 / candidate 1,208。バックグラウンド実行の結果を
  ポーリングする形の「待ち」が多い

### 失敗カテゴリ(上位)

| カテゴリ | 件数 |
|---|---|
| exec exit≠0 | 1,271 |
| hook 拒否: シェルでのファイル内容検索(grep 等) | 746 |
| hook 拒否: シェルでのファイル読み取り(cat 等) | 353 |
| hook 拒否: シェルでのファイル検索(find 等) | 242 |
| hook 拒否: その他の fileop | 265 |
| edit: old_string 不一致 | 89 |
| hook 拒否: 無条件 sleep | 51 |
| MCP ツールエラー | 47 |
| ユーザー割り込みによるキャンセル | 31 |

- exit≠0 を出しやすいコマンド: `pnpm --filter` 45%、`pnpm lint` 30%、`cmake` 17%、
  `cat` 13%(存在しないパス)、`mkdir`/`cp` 11%、`node script` 10%、`git commit` 10%
- `orca orchestration send` 5.6%、`orca orchestration check` 0.9%、`orca terminal read` 3.9%
- exit code: 1 が 1,072、2 が 104、127(command not found)24、128(git)24

### hook ブロック

- 拒否 1,657 件 / 286 セッション(全体の 58%)。1セッションあたり中央値 4・p90 11・最大 52
- ロール別 fileop 拒否: candidate 877 / coordinator 213 / reviewer 206 / integrator 161 / ad-hoc 149
- 週別の拒否率(ワーカーの tool call に占める割合):

| 週 | ワーカー | coordinator |
|---|---|---|
| 09/14(hook 導入は週半ば) | 2.25% | 1.95% |
| 09/21 | 4.27% | 4.08% |
| 09/28 | 4.42% | 8.74% |
| 10/05 | 4.59% | — |

→ **9/17 にテンプレへ「hook 遵守・代替手段」の定型句を入れた後も拒否率は下がっていない**
(P1 3.8% → P2 4.3%)。自然言語で注意しても効かない、という既知の結論をもう一度確認した形。

## 3. オーケストレーションのライフサイクル

### Orca メッセージ(週別)

| 週 | worker_done | heartbeat | question | escalation | status |
|---|---|---|---|---|---|
| 09/14 | 356 | 367 | 11 | 0 | 52 |
| 09/21 | 77 | 82 | 7 | 4 | 18 |
| 09/28 | 14 | 21 | 1 | 0 | 1 |
| 10/05 | 13 | 24 | 0 | 0 | 4 |
| 10/12 | 4 | 0 | 1 | 0 | 4 |
| 計 | 469 | 494 | 23 | 4 | 82 |

### ワーカーのライフサイクル遵守(セッションのコマンドから集計)

| 種別 | n | worker_done 送信 | heartbeat 0回 | ask 使用 |
|---|---|---|---|---|
| candidate | 301 | 97.3% | **22.3%** | 13 |
| integrator | 63 | 96.8% | **36.5%** | 1 |
| reviewer | 56 | 96.4% | **33.9%** | 5 |

- Orca 側でも 450 dispatch 中 133(30%)で `last_heartbeat_at` が NULL
- 最後の heartbeat から完了までの時間: 中央値 7.8分 / p90 37分 → heartbeat は死活監視として粗すぎる
- ask は 31回。question_threads 20件のうち回答済み 10件(平均 4.1分で回答)、
  **10件は未回答のまま close**。escalation は 1ヶ月で 4件
- 未 ack の delivery が 22件、fenced が 9件残っている

### dispatch の状態と所要時間

- dispatch_contexts: completed 434 / failed 16。retry は 2件だけ。
  failure_count≥1 は 6件
- worker_dispatches の失敗(16): agent_readiness timeout 3(9/15-16)・
  operator close 5・プロセス終了理由不明 2・**agent-trust-workspace による起動ブロック 1(Claude)**・stopped 6
- 開始からの残留リソースがある dispatch: 40件
- time-to-done(dispatched → completed、分):

| 区分 | n | p50 | p75 | p90 | max |
|---|---|---|---|---|---|
| 全体 | 429 | 20.4 | 39.0 | 74.9 | 373 |
| swe-2-medium | 136 | 14.8 | 26.5 | 52.0 | 158 |
| swe-2-high | 264 | 28.4 | 45.6 | 81.8 | 373 |
| P0 | 174 | 22.7 | 39.6 | 60.8 | 105 |
| P1 | 149 | 19.9 | 39.7 | 95.8 | 196 |
| P2 | 106 | 17.6 | 37.1 | 80.1 | 373 |

→ p90 が p50 の 3.7倍。テールが長く、統合が最遅ワーカー待ちになる構造は消えていない。

- 同時に動いていた dispatch の最大数: 全体 32(9/16)。9/15=26、9/17=26、9/18=15、
  9/21 以降は ≤9

### rate limit(Devin CLI ログより)

| 時刻(UTC時) | 最終失敗 ERROR | リトライ WARN | 影響プロセス数 |
|---|---|---|---|
| 09/16 04時 | 36 | 157 | 29 |
| 09/17 03時 | 21 | 88 | 20 |
| 09/17 04時 | 2 | 8 | 1 |
| 09/17 17時 | 7 | 29 | 7 |
| 9/21 以降 | 0 | 0 | 0 |

- 計 56 プロセスが free model rate limit に当たった(リセットまで 1〜15分と表示)
- **人間が手で再開させたプロンプト**(rate limit への言及あり)が 95件・59セッション
  (9/16=71、9/17=17、9/18=7)。再開系("Continue…")のプロンプトは別に 59件
- ワーカーへの人間の追送プロンプトは計 126件(74ワーカー)。完全自動ではなく、
  人間が復旧係をしていた
- 同じ時期に「HTTP 413 Payload Too Large で画像上限を下げて再試行」が 124件・72プロセス
  (スクリーンショットの溜め込み)、コンテキスト要約(継続スレッド化)が 204回・111セッション(23%)

### 承認メニュー停止 / zombie

- 492 セッション中 490 が `bypass` で起動しており、`normal` は 2件だけ。
  承認メニュー停止は spawn 時の強制でほぼ封じられた(直接の計測手段は無い)
- `[Pasted text` の残留を観測したのは 6セッション(注入はしたが送信されなかったケース)。
  agent_readiness timeout は 3件で、いずれも 9/16 以前
- 「未開始のまま放置された」dispatch は 0。現時点で dispatched のまま残っている 5件は進行中の OpenCode Run

## 4. coordinator の振る舞い

### 全 coordinator(27セッション)の主なコマンド

| 種類 | 回数 |
|---|---|
| sleep 呼び出し(合計時間) | 427回(7.2h)、うちループ内 95 |
| `orca orchestration check`(--wait / --types / --ack) | 257(186 / 185 / 68) |
| `orca terminal read`(画面読み) / `terminal send` | 449 / 100 |
| 生の `orca orchestration worker-start`(失敗) | 164(14) |
| `wait-for` | 49 |
| `declare -A` | 19(P0=13, P1=6, P2=0) |
| `orca eval` / `orca screenshot` | 25 / 16(P2 のみ) |

- sleep の長さ: 30s=68, 60s=50, 1s=43, 90s=23 …。ワーカー側の sleep 970回は 0〜4s が 79%
  (dev server 待ちなどの短い待ち)
- eval / screenshot の大半はワーカー側(eval 372 / screenshot 173)。
  coordinator が自分で実物を確認する回数は以前の実測(1Run で eval 328)よりずっと少ない

### orch サブコマンドの利用と失敗

| sub | 回数 | 失敗 | | sub | 回数 | 失敗 |
|---|---|---|---|---|---|---|
| status | 60 | 2 | | collect | 25 | 1 |
| wait | 49 | 1 | | integrate | 25 | 3 |
| adopt | 48 | 4 | | task | 24 | 4 |
| clean | 47 | 6 | | run | 21 | 0 |
| patrol | 42 | 1 | | compete | 9 | 0 |
| batch | 29 | 4 | | estimate | 5 | 0 |
| spawn | 26 | 7 | | score / weights | 1 / 1 | 0 |

失敗約40件の内訳:
- **usage エラー(引数の誤り)が 13** と最多(clean 5・adopt 3・spawn 2・integrate 2・batch 1)
- 並列 cap による拒否 3(設計どおり)
- run 未作成 2
- PATH に orch が無い 1
- 存在しないサブコマンドの呼び出し 2(`ack` など)

`orca orchestration send` の失敗 56件の内訳:
- **capability revoke 後の送信が 20**(救出された dispatch が worker_done を送ろうとして弾かれる)
- taskId 欠落 5
- capability トークン欠落 4
- reply の使い方を誤る 5

## 5. 対策導入前後の比較

| 指標 | P0(〜9/17) | P1(9/17〜9/21) | P2(9/21〜) | 判定 |
|---|---|---|---|---|
| coordinator 数 / ワーカー数 | 12 / 164 | 6 / 148 | 9 / 108 | |
| coordinator の sleep 合計 | 5.15h | 1.18h | 0.89h | 改善 |
| sleep 時間 / coordinator 生成時間 | 0.19 | 0.06 | 0.05 | **約1/4に減少** |
| `check --wait` + `orch wait` | 52 + 0 | 60 + 13 | 74 + 36 | 改善(イベント駆動化) |
| orch 経由の操作 | 0 | 110 | 309 | 定着 |
| 生 worker-start(失敗) | 79(5) | 60(7) | 25(2) | 減ったが残る |
| `declare -A` | 13 | 6 | 0 | **解消** |
| rate limit ERROR | 36 | 30 | 0 | 改善(並列度の低下と交絡) |
| ワーカーの hook 拒否率 | 0%(hook 無し) | 3.8% | 4.3% | **効果なし** |
| heartbeat / worker_done(ワーカー) | 168/186 | 189/152 | 121/100 | 横ばい |
| escalation | 0 | 0 | 2 | ほぼ未使用のまま |
| TTD p90(分) | 61 | 96 | 80 | 改善なし(LPT の実投入は 5 estimate のみ) |

まとめ:
- 効いた: 機械的な強制(wait コマンド・batch manifest・state 永続化)
- 効かなかった: 自然言語の注意書き(hook 遵守・heartbeat・escalation)。
  sessions.db 分析の原則と一致する

## 6. 分類: 汎用問題 vs Devin 固有問題(最重要)

### (a) どのエージェントでも再発する汎用問題

| # | 問題 | 実測 | 汎用ハーネスでの扱い |
|---|---|---|---|
| G1 | 共有アカウント/プロバイダの rate limit による一斉停止 | 56プロセス、人手再開 95件 | プロバイダ単位の並列 cap + **画面/ログでの rate limit 検出 → 自動バックオフ再開** |
| G2 | coordinator が盲目的に sleep して待つ | P0 5.15h → P2 0.89h | `orch wait` + coordinator 専用の sleep ブロック。どのエージェントでも有効 |
| G3 | heartbeat を送らないワーカー | 22〜37% が 0回、hb→完了 p50 7.8分 | 死活はハーネス側で見る(端末出力/ログの最終活動時刻)。heartbeat は任意扱い |
| G4 | ask / escalation を使わない・質問が放置される | ask 31、escalation 4、未回答 close 10/20 | 曖昧 spec の事前ゲート、質問 SLA(未回答 N分で coordinator を起こす) |
| G5 | coordinator がハーネスを迂回して生 API を叩く | P2 でも生 worker-start 25 | wrapper/hook で生 dispatch API を拒否するか、orch の側に寄せる |
| G6 | CLI の引数ミス | orch 失敗の 1/3 が usage | 例付き `--help`・寛容なパーサ・JSON 入力・サブコマンドの別名 |
| G7 | シェル方言の罠(bash 3.2 `declare -A`) | P0 13 → P2 0 | 対応表は JSON manifest(解決済み・汎用) |
| G8 | 救出/再起動後の完了報告が capability revoke で弾かれる | send 失敗 20 | 報告をファイル(report.md)にも出させ、ハーネスが照合して取り込む |
| G9 | 未 ack の配信・残留リソース | outstanding 22 / fenced 9 / residual 40 | 定期的な reconcile(collect/clean)を機械化 |
| G10 | 所要時間のロングテール | TTD p90/p50=3.7 | 重さ推定 + LPT 投入、早期採否、クラスタ単位の pipeline |
| G11 | 自然言語のルールが守られない(hook を再学習し続ける) | 拒否率 2.3%→4.3% | ブロックより書き換え/許可リスト、または各エージェントの専用ツールへ誘導 |
| G12 | コンテキスト肥大(要約・画像) | 要約 23% セッション、413 124件 | 仕事を小さく切る、スクショ予算、成果物は report に外出し |
| G13 | 人間が復旧係になる | 追送 126件 | 自動 resume / rescue を標準経路にする |
| G14 | 起動時の workspace trust ゲート | Claude で 1件 | エージェントごとの起動前チェック(→ 下の adapter で吸収) |

### (b) Devin / SWE-2 固有の問題

| # | 問題 | 汎用化の方針 |
|---|---|---|
| D1 | TUI の準備完了判定を `SWE-2 (Medium\|High\|Max)` ステータスバーの文字列で行っている | adapter の `ready_probe`(正規表現/コマンド)に移す |
| D2 | Orca の `terminal wait --for tui-idle` が devin を known-agent と認識しない | Orca がネイティブ対応しているエージェントは `worker-start --agent` を使う。未対応のものだけ画面ポーリング |
| D3 | 注入したプロンプトが `[Pasted text]` のまま送信されない(Enter 未送信) | adapter の `submit_verify`(入力欄が空になったか確認)。貼り付けの扱いは TUI ごとに違う |
| D4 | 承認回避フラグ `--permission-mode bypass` | adapter の `unattended_flags`(Claude: `--dangerously-skip-permissions` 系、Codex: approval/sandbox 設定 など) |
| D5 | モデル名 `swe-2-medium/high/max` と `--allow swe-2-max` ゲート | 抽象的な tier(low/mid/high/max)→ エージェントごとのモデル名に対応付け。max ゲートは tier に対して行う |
| D6 | rate limit 文言("Reached free model rate limit"、"resets in N minutes")と free model 固有の上限 | adapter の `rate_limit_patterns` + 待機時間の抽出 |
| D7 | `sessions.db` のスキーマ(prompt_history / message_nodes / tool_call_state / `_meta.cognition.ai/*`)と lock ファイルを使った生存カウント | adapter の `telemetry_reader`。共通は orch 自身が書くイベントログ |
| D8 | Devin hooks の形式、fork が親の hook セットを引き継ぐ | hook はエージェントごとに設定生成。ポリシーは共通定義 |
| D9 | ツール名(exec/get_output/find_file_by_name)とバックグラウンド shell のポーリング | 計測の正規化レイヤでツール種別に写像する |
| D10 | 413 で画像上限を下げて再試行する挙動 | Devin バックエンド固有。汎用側は G12 で扱う |
| D11 | dispatch の 91%(410/450)が「terminal を作って devin を手動起動 → worker-start --terminal」経路 | 他エージェントは Orca ネイティブ経路で起動できる(実績: opencode 9 / claude 3 / antigravity 7) |

**境界のケース**: hook による fileop ブロック(G11)は「各エージェントの専用ツールに
誘導したい」という汎用の動機を持つが、ブロックする内容(どのツールを使わせるか)は
エージェントごとに違う → ポリシーは汎用、ルールの中身は adapter で持つ。

## 7. 汎用ハーネスへの推奨(優先順)

1. **Agent Adapter 層を導入する**。`agents/<name>.json` に次を定義する:
   起動コマンド、無人実行フラグ、ready_probe、submit_verify、tier→モデル名、
   rate_limit_patterns、telemetry_reader、hook 生成。
   D1〜D9 をここに閉じ込め、bin/orch 本体から Devin 固有の文字列を消す。
   Orca ネイティブの `--agent` があるエージェントはそちらを優先する
2. **rate limit の自動バックオフと自動再開**。プロバイダ単位で並列 cap を持ち、
   画面/ログで rate limit を検出したら待ってから resume プロンプトを自動送信する
   (人手の再開 95件・追送 126件をなくす)
3. **死活監視をハーネス側の観測に一本化する**。端末出力の最終変化時刻・プロセス生存・
   telemetry の最終活動を見る。heartbeat は補助に格下げ(0回が 22〜37%)。
   `patrol` を定期起動して zombie / stalled / rate-limited を分類する
4. **coordinator の待ちは `orch wait` だけにする**(sleep ブロックは coordinator ロールに限定)。
   実測で sleep が約1/4になった施策なので、全エージェントに標準で入れる
5. **生 dispatch API の迂回を塞ぐ**。coordinator 環境で `orca orchestration worker-start` を
   直接叩いたら wrapper/hook で orch に誘導する(P2 でも 25件残っている)
6. **完了報告を二重化して reconcile する**。worker_done と report.md/コミットを照合し、
   capability revoke(20件)や未 ack(22件)でも成果を失わないようにする。
   residual リソースは clean で機械的に回収する
7. **CLI のエルゴノミクスを上げる**。usage エラーが orch 失敗の 1/3 を占める。
   エラーに正しい例を出し、よく間違える形(位置引数/フラグ)を受け入れ、
   サブコマンドの別名(`ack` など)を用意する
8. **質問の SLA と曖昧性ゲート**。未回答の question(10/20)が N 分続いたら
   coordinator を起こす。曖昧 spec × 低 tier の拒否(Jev ゲート)は tier ベースで汎用化する
9. **ポリシーは共通・ルールはエージェント別の hook 生成**。拒否の文言で再学習させるより、
   許可される代替コマンドへの自動書き換えか、spec 冒頭での予告にする。
   拒否率はエージェント別に計測し続ける
10. **計測を正規化する**。orch 自身が `.orch/events.jsonl`(spawn/ready/done/fail/rate_limit/
    tokens)を書き、重さ推定・LPT・前後比較を Devin `sessions.db` に依存させない。
    adapter の telemetry_reader はトークン数などの補完に使う

## 付録: 数字の出どころ

- 集計スクリプト: セッションの scratchpad(repo 外)。DB は全て
  `mode=ro&immutable=1` で開いた
- ロール推定の誤差: dispatch に結合できなかったセッション 29件と、spec の分類
  ヒューリスティックによる誤分類があり得る
- rate limit は Devin CLI ログの WARN/ERROR 行から数えた。sessions.db の
  ツール出力にも 18件(12セッション)ある(coordinator が画面で見たもの)
