# エージェント・アダプタ

orch が特定のエージェント CLI について知っていることは、全て `adapters/<id>.json`
のデータにある(起動 argv・バイパスフラグ・準備完了/承認/レート制限の画面シグネチャ・
tier→モデル・ログの場所・hook の入れ方)。コード側(`lib/orchlib/adapters.py` と
`bin/orch`)はそれを解釈する汎用ロジックだけ。`declare -A` 事故と同じ理屈で、
対応表はデータにしておけば監査でき、壊れていればロード時に落ちる。

同梱: `devin`(旧ハーネスの挙動をそのまま移植した基準)、`claude`、`codex`、
`opencode`、`generic`(未知 CLI の雛形)。現状は `orch agents` で見る。

## 探索順

1. `$ORCH_ADAPTERS_DIR`(`:` 区切りで複数可)
2. `<cwd>/.orch/adapters/`(repo ごと)
3. ハーネスの `adapters/`

同じ id は先に見つかった方が勝つ(同梱の上書きもできる)。`_` で始まるファイルは無視。
ロード時に `provider` / `binary` / `launch` / `models` / `screen` の存在、
tier 3つ(medium/high/max)の存在、`screen.*` の正規表現のコンパイルを検証する。

## フィールド

| キー | 意味 |
|---|---|
| `id` / `description` | 識別子(ファイル名と同じにする)と説明 |
| `provider` | rate limit を共有するアカウント単位。並列 cap はこの単位(`--cap anthropic=6`) |
| `binary` / `binary_env` | 実行ファイル。`binary_env`(例 `ORCH_CLAUDE_CMD`)が設定されていれば優先(引数付き文字列も可) |
| `process_names` / `liveness` | 生存数の数え方。`ps`(プロセス名で数える)/ `devin_locks`(devin の lock+sessions.db)/ `none` |
| `activity_globs` | `ps` の補正。直近 `ORCH_PARALLEL_IDLE_MIN` 分に書かれたファイル数と min を取り、放置セッションを数えない |
| `launch.argv` | 起動テンプレ。`{binary}` `{model_args}` `{effort_args}` `{bypass}` `{hook_args}` を展開 |
| `launch.bypass` | 承認回避フラグ。**無人ワーカーの必須条件**(空なら `binary` 側に含める) |
| `launch.model_args` / `effort_args` | `{model}` / `{effort}` を含むテンプレ。値が無ければ丸ごと落ちる |
| `launch.env` | 起動時に付ける環境変数 |
| `models.tiers` | `medium` / `high` / `max` → `{"model": ..., "effort": ...}`。空 `{}` = argv でモデルを変えない(opencode) |
| `models.default_tier` | 指定子が空の時の tier |
| `models.known` | 既知のモデル id。agent 無しで指定された id の逆引きに使う |
| `models.gated_models` | max 扱いにするモデル id(tier 名に依らず gate) |
| `models.fuzzy` | CLI が部分一致でモデル解決する場合 true。`max` を含む指定・gated id の接頭辞も gate |
| `screen.*` | 画面 tail に当てる正規表現(下記) |
| `readiness` | `strategy`(下記)と `timeout_s` |
| `resume_text` | rate limit からの再開時に送る文 |
| `transcript.reader` | トークン等の補完に使う reader(下記)。null なら `.orch/events.jsonl` の所要時間だけ |
| `hooks.installer` | sleep hook の入れ方(下記) |
| `spec_notes` | spec 末尾に「## エージェント固有の注意 (<id>)」として自動追記する定型句 |
| `verified` | 実機確認の有無。`false` の項目は `orch agents` の `unverified` に出る |

### 画面シグネチャ (`screen`)

| キー | 用途 |
|---|---|
| `ready` | 入力を受け付けられる状態(準備完了判定) |
| `input_echo` | 未送信の入力欄。`{first}` はプロンプト先頭30字に置換。送信後これが消えたら成功、残ればリトライ。空なら検証なし(1回送信+paste 救出のみ) |
| `pending_paste` | `[Pasted text #N]` 等の未送信 paste。見えたら Enter を送る |
| `approval` | 承認メニュー。patrol が `permission-menu` を報告 |
| `approval_answer` | 承認メニューの番号付き選択肢のうち、これにマッチするラベルの番号を `--rescue` で押す(例 `bypass`、`don't ask again\|always`)。正規表現検証の対象外 |
| `rate_limit` | レート制限表示。patrol が `rate-limited` を報告→provider の cap を半減して state の `caps_auto` に永続化(env/`--cap` より優先される上限)→`--rescue` で `--cooldown-min`(既定10)後に `resume_text` を送る |
| `startup_prompts` | `[{"match": 正規表現, "keys": [...]}]`。workspace trust・バイパス確認・アップグレード通知など。準備待ち中に自動応答(最大4回)、起動後に出たら patrol `--rescue` が押す。`keys` は `"enter"` か送信する文字列 |

### 準備完了 (`readiness.strategy`)

- `screen-regex` — `screen.ready` を2秒ごとにポーリング(startup_prompts に応答しながら)
- `orca-tui-idle` — Orca の `terminal wait --for tui-idle`。Orca が知っているエージェントにだけ有効
- `both` — tui-idle の後に regex で確認
- `screen.ready` が空なら strategy に関係なく tui-idle にフォールバック

タイムアウトしても worker-start は試行する(警告と画面 tail を stderr に出す)。

### transcript reader / hook installer

| reader | 読む場所(env で上書き) |
|---|---|
| `devin_sessions_db` | `~/.local/share/devin/cli/sessions.db`(`ORCH_DEVIN_DB`) |
| `claude_projects` | `~/.claude/projects/*/*.jsonl`(`ORCH_CLAUDE_PROJECTS`) |
| `codex_rollout` | `$CODEX_HOME` / `~/.codex` / Orca の codex runtime home の `sessions/**/rollout-*.jsonl`(`ORCH_CODEX_HOME`) |
| `opencode_sqlite` | `~/.local/share/opencode/opencode.db`(`ORCH_OPENCODE_DB`) |

ワーカーのプリアンブルの task ID で Orca の task と結合する。失敗時は空を返すだけ(advisory)。

| installer | 動作 |
|---|---|
| `launch-args` | claude。起動ごとに `--settings '<hooks JSON>'` を足す。グローバル設定に触らないので install 不要 |
| `global-config` | devin。`~/.config/devin/config.json` の `hooks.PreToolUse` に merge |
| `opencode-plugin` | `~/.config/opencode/plugins/` にプラグインを生成(`hooks/opencode/` のテンプレ) |
| `codex-hooks-json` | `$CODEX_HOME/hooks.json`(既定 `~/.codex`)に merge。実験的 |
| null | hook 無し。sleep 禁止は spec_notes と playbook 頼み |

`orch hooks install --agent X` は dry run(書き込み先と内容を表示)、`--apply` で書く。
既存ファイルは `.bak-orch-<時刻>` にバックアップしてから atomic に置き換える。
ポリシー本体は全エージェント共通の `hooks/no_blind_sleep.py --agent <id>`。

## モデル指定子

```
[<agent>:]<tier|model-id>
```

- `claude:high` — claude の high tier(= opus)
- `high` — 既定エージェントの high。既定は `--agent` > `ORCH_AGENT` > run state(`orch boot --agent`)> devin
- `swe-2-high` — `models.known` にある id は agent を逆引き(→ devin)。後方互換
- `codex:gpt-6-luna` — model id の直接指定。agent 付きなら known に無い id もそのまま渡る(tier は tiers の model と一致すれば付く。effort は付かない)
- 空 — 既定エージェントの `default_tier`

`spawn/compete/integrate/agents --check` の `--agent X` と、manifest の
`defaults.agent` / item の `agent` は、agent 無しの指定子にだけ前置される。

### max gate (`--allow`)

tier が max、または `gated_models` にある(fuzzy なら部分一致も)指定は、
run の allow に含まれない限り spawn/compete/batch/integrate が die する。
`orch boot --allow` の値(カンマ区切り):

| 値 | 意味 |
|---|---|
| `max` | 全エージェントの max |
| `claude` / `claude:max` | claude の max |
| `swe-2-max` | 後方互換。`devin:max` |

allow は run の `.orch/state.json` に永続化され、`ORCH_ALLOW` env と OR で判定する。
coordinator は自分のエージェントの max が allow されていれば max、そうでなければ high で起動する。

確認: `orch agents --check <指定子>` は解決結果・実際の起動コマンド・gate 有無を出す(端末は作らない)。

```
$ orch agents --check claude:high
{"resolved": {"agent": "claude", "tier": "high", "model": "opus", "effort": null, "spec": "claude:high"},
 "command": "claude --model opus --dangerously-skip-permissions --settings '{\"hooks\": ...}'",
 "gated": false}
```

## 新しいエージェントを足す

`.orch/adapters/<id>.json`(repo 限定)か `$ORCH_ADAPTERS_DIR/<id>.json` に置く。
`adapters/generic.json` をコピーして埋めるのが早い。例: Antigravity
(Orca の既知エージェント。月次ログでも 7 dispatch で使われていた。
バイパスは Orca のフラグ表にある `--dangerously-skip-permissions`)。

```json
{
 "id": "antigravity",
 "description": "Antigravity CLI (Google)",
 "provider": "google",
 "binary": "agy",
 "binary_env": "ORCH_ANTIGRAVITY_CMD",
 "process_names": ["agy"],
 "liveness": "ps",
 "launch": {
  "argv": ["{binary}", "{model_args}", "{effort_args}", "{bypass}"],
  "bypass": ["--dangerously-skip-permissions"],
  "model_args": ["--model", "{model}"],
  "effort_args": ["--effort", "{effort}"],
  "env": {}
 },
 "models": {
  "default_tier": "medium",
  "tiers": {
   "medium": {"model": "gemini-4-argon-medium"},
   "high": {"model": "gemini-4-argon-high"},
   "max": {"model": "gemini-4-argon-high", "effort": "max"}
  },
  "known": ["gemini-4-argon-medium", "gemini-4-argon-high", "gemini-4-argon-low",
            "gemini-3.8-flash-high", "gemini-3.8-flash-medium", "gemini-3.8-flash-low"],
  "gated_models": [],
  "fuzzy": false,
  "list_source": {"cmd": ["agy", "models"]}
 },
 "screen": {
  "ready": "",
  "input_echo": "",
  "pending_paste": "\\[Pasted text",
  "approval": "allow once|allow always|permission required",
  "approval_answer": "always",
  "rate_limit": "RESOURCE_EXHAUSTED|Quota exceeded|429",
  "startup_prompts": [{"match": "Do you trust", "keys": ["enter"]}]
 },
 "readiness": {"strategy": "orca-tui-idle", "timeout_s": 120},
 "resume_text": "continue — 中断した作業を再開し、ライフサイクル義務(heartbeat / worker_done)を守ること",
 "transcript": {"reader": null},
 "hooks": {"installer": null},
 "spec_notes": "- sleep で待たない。条件ループ(until ...; do sleep 2; done)か orca terminal wait を使う",
 "verified": {"launch": false, "ready": false, "approval": false, "rate_limit": false, "hooks": false}
}
```

tiers のモデルは `agy models`(2026-10-18 取得)の最新世代 `gemini-4-argon-*`。
agy のモデル id は effort 込み(`-low/-medium/-high`)で max 版が無いので、max tier は
`-high` に `--effort max` を足している(id の接尾辞と `--effort` のどちらが優先されるかは未確認)。
`gemini-3.8-flash-*` は軽い仕事向けに known にだけ入れた(`antigravity:gemini-3.8-flash-medium` で指定)。
tiers を空(`{}`)にすると `--model` を渡さず CLI の設定に従う。いずれにせよ、
モデル id は下の「モデル id は最新を参照する」の手順で**その時点の**一覧から選ぶ。

注意:
- 画面文言は例。手元の CLI で確認して差し替える(`verified` は確認するまで false)
- `max` を他 tier と同じモデルにする場合でも、max tier の指定は gate される
- node 製 CLI はプロセス名が `node` になりうる。`process_names` に `node` を入れると
  他の node プロセスも数えるので、`activity_globs` を足すか cap を控えめにする
- provider を既存と同じにすると cap を共有する(同じアカウントなら同じにする)

確認手順: `orch agents`(found / tiers / unverified / stale_models)→
`orch agents --check antigravity:high`(起動コマンド)→ 下のスモーク。
単体テストは `python3 -m unittest discover tests`。

## モデル id は最新を参照する

モデル id はアダプタの JSON より速く入れ替わる(このハーネスの最初の PR でも
codex の tier が既に古かった)。アダプタを書く・直すときは、記憶や過去の
ドキュメントではなく**必ずその時点の最新情報**を見て決める。

| エージェント | 最新一覧の見方 |
|---|---|
| codex | `codex debug models`、または `~/.codex/models_cache.json` の `visibility: "list"` |
| devin | `devin models list` |
| opencode | `opencode models` |
| antigravity | `agy models` |
| claude | `claude --help` のエイリアス(`sonnet` / `opus` / `fable`)。フル ID は公式ドキュメント |
| その他 | 各 CLI の models コマンドか公式ドキュメント |

- tier にはできるだけ**エイリアス**(常に最新を指す名前)を使う。claude はそうしている
- 世代が新しいモデルがあれば全 tier をそれに揃え、tier の差は effort で付ける(codex は medium/high/max とも `gpt-6-luna`)。ローカル設定ファイルの既定モデルは古いことがあるので根拠にしない
- `models.list_source` を書いておくと、`orch agents` がその一覧と tier を突き合わせ、
  一覧から消えたモデルを `stale_models`(例: `high=gpt-5.5`)として出す。
  一覧が取れない環境では `models_checked: false` になるだけで止まらない
  - `{"json": ["~/.codex/models_cache.json"], "key": "models", "field": "slug", "skip_if": {"visibility": "hide"}}`
  - `{"cmd": ["opencode", "models"]}`(1行1モデル、先頭トークンを id とみなす)
- 定期的に `orch agents` を回し、`stale_models` が出たら tiers を更新する

## 未検証チェックリスト

`verified: false` / 実機未確認のもの。確認したらアダプタの `verified` と正規表現を更新する。

- [ ] codex: `hooks.json` のスキーマ(シェルツール名・`tool_input` の形・ブロックの意味)。
      Orca が起動する codex は Orca の runtime home(`CODEX_HOME`)を使い、hooks.json を
      書き換える可能性がある → `CODEX_HOME=<runtime home> orch hooks install --agent codex --apply`
- [ ] claude: rate limit 表示の文言(`usage limit reached` / `limit reached ∙ resets` 等)、承認メニューの文言
- [ ] codex: rate limit 表示の文言
- [ ] opencode: `--auto` 時に承認画面が出るか・その文言、ready 判定、プラグインの `throw` でツール実行が止まるか
- [ ] Orca `terminal wait --for tui-idle` が devin を known-agent と認識するか
      (現状 devin は `screen-regex` なので影響なし。認識するなら `both` に寄せられる)
- [ ] 実起動スモーク(claude / codex / opencode 各1回。有料セッションが立つ):
  1. 使い捨て repo で `orch init` → `orch run create "smoke"`
  2. `orch agents --check <agent>:medium` で起動コマンドを目視
  3. 1ファイル追加+コミット+worker_done だけの spec を `tasks/smoke.md` に書く
  4. `orch compete tasks/smoke.md -n 1 --models <agent>:medium --cluster smoke`
     (端末が立ち、trust ゲートが自動応答され、プロンプトが送信されること)
  5. `orch wait --timeout-min 20` で worker_done が来ること。並行して `orch patrol` に
     `permission-menu` / `startup-gate` が出ないこと
  6. ワーカー端末で `sleep 60` を打たせ hook がブロックすること(opencode/codex は install 後)
  7. `orch collect`(dirty=0・ブランチにコミット)→ `orch clean --all` → `orch status` で残骸ゼロ
  8. `.orch/events.jsonl` に spawn / done / clean があること、`orch weights` に行が出ること
