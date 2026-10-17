# swe2-orca-orchestrate

Orca + 任意の AI コーディングエージェント(Claude Code / Codex / OpenCode /
Devin …)向けのマルチエージェント・オーケストレーションハーネス。
設計と失敗対策の一覧は [AGENTS.md](AGENTS.md)、エージェントの追加は
[docs/adapters.md](docs/adapters.md)。

```bash
export PATH="$PWD/bin:$PATH"     # 任意
orch agents                      # 手元で使えるエージェントと tier→モデル対応
orch agents --check codex:high   # spawn が打つ起動コマンドを確認(起動はしない)
orch hooks install --agent opencode --apply   # sleep hook を永続導入(claude は不要)

cd <対象repo> && orch init       # 対象repoのルートで
orch boot requirement.md --agent claude --cap anthropic=6,openai=4
# 手動なら: orch run create "<目的>" → orch compete tasks/x.md -n 3 \
#   --models claude:high,codex:medium,devin:medium → orch wait
```

モデル指定子は `[<agent>:]<tier|model-id>`(tier = medium / high / max)。
max は `orch boot --allow <agent>:max`(全エージェントなら `--allow max`)した Run でのみ使える。
既定エージェントは devin(`--agent` / `ORCH_AGENT` / `boot --agent` で変更)。

テスト: `python3 -m unittest discover tests`
