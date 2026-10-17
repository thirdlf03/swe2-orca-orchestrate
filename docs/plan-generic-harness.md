# 汎用ハーネス化プラン — Orca + 任意の AI コーディングエージェント

根拠: [docs/findings/devin-month-report.md](findings/devin-month-report.md)
(1ヶ月・492セッション・dispatch 450件の集計)と、Orca 1.4 の既知エージェント
カタログ(46種)・CLI 各種の調査。

## 0. 方針

1. **失敗対策の機械化はそのまま残す**。Devin で測った失敗のうち 12 系統は
   エージェントに依らず再発する(レポート §6 の汎用問題)。対策をエージェント
   依存部から切り離し、全アダプタで効くようにする。
2. **エージェント固有の知識はデータにする**。起動 argv・バイパスフラグ・
   準備完了/承認/レート制限の画面シグネチャ・モデル tier・ログの場所は
   `adapters/<id>.json` に書く。コードはアダプタを解釈する汎用ロジックだけ。
   Devin の `declare -A` 事故と同じで、対応表はデータとして監査できるのが安全。
3. **Orca を正とする**。Orca が状態を把握できる場合は(`worker-show` の
   `observation.agentWait`、hooks)そちらを優先し、画面 regex はフォールバック。
4. **後方互換**。既定エージェントは devin。既存の `--model swe-2-high` や
   `--allow swe-2-max` はそのまま動く。

## 1. エージェント指定

- モデル指定子: `[<agent>:]<tier|model-id>`
  - tier は `medium` / `high` / `max`(全アダプタ共通の語彙。playbook の
    モデル配分表がそのまま使える)。アダプタが tier を実モデル+effort に写す
  - 例: `claude:high`、`codex:medium`、`devin:swe-2-high`、`high`(既定エージェント)
  - 既知のモデル id(例 `swe-2-max`、`opus`)からはエージェントを逆引きする
- 既定エージェントの決定順: `--agent` フラグ > `ORCH_AGENT` env >
  run の `agent`(`orch boot --agent X` が state.json に永続化)> `devin`
- `orch compete spec.md -n 3 --models claude:high,codex:high,devin:medium`
  のように**エージェント混合コンペ**ができる

## 2. アダプタ (`adapters/<id>.json`)

```jsonc
{
  "id": "claude", "provider": "anthropic", "binary": "claude",
  "process_names": ["claude"],
  "launch": {"argv": ["{binary}", "{model_args}", "{bypass}", "{hook_args}"],
             "bypass": ["--dangerously-skip-permissions"],
             "model_args": ["--model", "{model}"], "effort_args": ["--effort", "{effort}"],
             "env": {}},
  "models": {"default_tier": "medium",
             "tiers": {"medium": {"model": "sonnet"}, "high": {"model": "opus"},
                       "max": {"model": "opus", "effort": "max"}},
             "known": ["opus", "sonnet", "haiku", "fable"], "gated_tiers": ["max"]},
  "screen": {"ready": "...", "input_echo": "^>\\s*{first}", "pending_paste": "...",
             "approval": "...", "approval_answer": "bypass|don't ask|always",
             "rate_limit": "...", "startup_prompts": [{"match": "...", "keys": ["enter"]}]},
  "readiness": {"strategy": "screen-regex|orca-tui-idle|both", "timeout_s": 180},
  "resume_text": "continue",
  "transcript": {"reader": "claude_projects"},
  "hooks": {"installer": "settings-flag", "pre_tool_matcher": "Bash"},
  "spec_notes": "このエージェント向けの定型句(ツール名・hook の代替手段)"
}
```

同梱: `devin`(現行挙動を完全移植)、`claude`、`codex`、`opencode`、
`generic`(未知 CLI 用。`ORCH_GENERIC_CMD` 等で上書き)。
ユーザー定義は `$ORCH_ADAPTERS_DIR` または `<repo>/.orch/adapters/` に置けば
同梱より優先。

## 3. 起動経路

既定 (A): `worktree create` → `terminal create --command <adapter argv>` →
準備完了待ち(アダプタの strategy。startup_prompts の自動応答=信頼ゲート対策を含む)→
`worker-start --task --terminal --worktree` → 未送信 paste の救出 → 失敗時は全ロールバック。
バイパス・モデル・hook をハーネス側で確実に制御できるので、Orca の
`agentDefaultArgs` に依存しない(調査で claude/codex は未設定=承認プロンプトで止まると判明)。

## 4. エージェント非依存の機構(レポートの推奨順)

| # | 推奨 | 実装 |
|---|---|---|
| 1 | アダプタ層 | `lib/orchlib/adapters.py` + `adapters/*.json`、`orch agents` で一覧/検出 |
| 2 | プロバイダ単位の並列 cap + レート制限検出/自動復帰 | cap は provider ごと(`caps: {anthropic: 6}`、既定は全体 cap)。patrol が rate_limit 画面を検出→`rate-limited` issue+event+その provider の cap を自動で半減、`--rescue` で cooldown 後に resume_text を送る |
| 3 | ハーネス側の死活判定 | patrol が `observation.agentWait` → 承認/レート制限/信頼ゲート regex → 経過時間の順に判定。heartbeat は任意扱い |
| 4 | `orch wait` 標準化 | 既存のまま。playbook で強調 |
| 5 | 生 `worker-start` 抑止 | playbook とエラー文で `orch spawn` 誘導(機械的抑止は Orca 側なので不可) |
| 6 | 完了の二重記録と突合 | `orch wait` が worker_done を `.orch/events.jsonl` に記録。`orch collect` が dirty と commit 数を突合 |
| 7 | CLI の使いやすさ | 未知フラグ・引数不足時に usage 例を出す |
| 8 | 曖昧 spec × medium tier の gate | Jev gate を「tier==medium」判定に一般化 |
| 9 | hook ポリシー共通化+エージェント別生成 | `orch hooks install --agent X`(claude は起動ごと `--settings` 注入、devin はグローバル設定 merge、opencode はプラグイン生成、codex は hooks.json 生成=実験的)|
| 10 | 自前イベントログ | `.orch/events.jsonl`(spawn/done/adopt/clean/rate_limit/patrol)。重さ推定は「トランスクリプト指標(あれば)→イベントの所要時間」の順で、全エージェントで動く |

## 5. モジュール分割(並列実装の単位)

| 単位 | ファイル | 担当 |
|---|---|---|
| アダプタ・コア | `adapters/*.json`, `lib/orchlib/adapters.py`, `lib/orchlib/events.py`, `bin/orch` | コーディネーター |
| トランスクリプト指標 | `lib/orchlib/metrics.py`, `tests/test_metrics.py` | サブエージェント M |
| hook 汎用化 | `hooks/no_blind_sleep.py`, `lib/orchlib/hooks.py`, `hooks/opencode/`, `tests/test_hooks.py` | サブエージェント H |
| 文書・テンプレ | `AGENTS.md`, `README.md`, `playbooks/coordinator.md`, `tasks/*.md`, `spec/run.example.json` | コア完了後 |

### 契約: `lib/orchlib/metrics.py`

```python
def collect(reader: str, task_ids: set | None = None) -> dict[str, dict]:
    """task_id -> {"agent","session","model","in_tok","out_tok","gen_ms","started","ended"}.
    reader は adapters の transcript.reader("devin_sessions_db" | "claude_projects" |
    "codex_rollout" | "opencode_sqlite")。task_id はワーカープリアンブルの
    `task ID is: <id>` で結合。失敗・データ無しは {} を返し例外を出さない(advisory)"""
```

### 契約: `lib/orchlib/hooks.py`

```python
def launch_args(agent_id: str, harness_dir: str) -> list[str]   # 起動 argv に足す引数(claude: --settings JSON)
def install(agent_id: str, harness_dir: str, dry_run: bool) -> dict  # 永続インストール(devin/opencode/codex)
```

## 6. 検証

- `python3 -m unittest discover tests`(アダプタの argv 生成・モデル解決・gate・
  画面 regex をサンプル画面で、hook の各エージェント入力形式、metrics を合成フィクスチャで)
- 実データでの読み取り確認: `orch agents`、`orch weights`、`orch estimate`
  (read-only。devin と claude のローカルログが対象)
- Orca 上での実起動スモーク(claude アダプタで spawn→worker_done→clean)は
  有料セッションを起動するため本作業では行わず、手順を README に残す

## 7. 未検証事項(アダプタに `verified: false` で明記)

- codex の hooks.json スキーマ、claude/codex のレート制限文言、opencode の承認画面
- `terminal wait --for tui-idle` が Orca 1.4.224 で devin を認識するか
