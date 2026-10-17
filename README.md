# vibe-trace-cleaner

Extract, **anonymize**, segment and quality-score your local coding-agent session traces — and turn them into a clean, submittable dataset.

Built for contributors of [saidutta69/agentic-vibecoding-traces](https://huggingface.co/datasets/saidutta69/agentic-vibecoding-traces), but works standalone on any machine.

> **Using a coding agent?** Point it at [`AGENTS.md`](AGENTS.md) — the repo's DOX contract. It documents what each script owns, the exact workflow, the anonymization + schema contracts, and how to verify changes, so an agent can pick up the work without extra context.

> **Privacy first.** Everything runs 100% locally. Nothing is uploaded anywhere by this tool. You review the output before you send it.

<div align="center">
  <img src="https://photu.kashyalabanavli.site/racer-is-op.png" alt="RACER IS OP" width="100%">
</div>

## Supported agents (auto-detected)

| Agent | Reads from |
|-------|-----------|
| opencode | `~/.local/share/opencode/opencode.db` |
| Claude Code | `~/.claude/projects/**/*.jsonl` |
| Grok CLI | `~/.grok/sessions/**/chat_history.jsonl` |
| Manicode / Freebuff | `~/.config/manicode/projects/*/chats/*/chat-messages.json` |

macOS + Linux. Python 3.10+.

## Quickstart

```bash
git clone https://github.com/instax-dutta/vibe-trace-cleaner
cd vibe-trace-cleaner
pip install -r requirements.txt

cp blocklist.example.txt blocklist.txt   # EDIT THIS (see below)
python vibe_clean.py --out cleaned_output
```

### 1. Fill in your blocklist (important)

Open `blocklist.txt` and add your personal identifiers, one per line — real name, usernames/handles, org names, project codenames:

```
Jane Doe
janedoe_dev
MyStartupName
secret-project
```

Everything on this list is replaced with `[REDACTED]` everywhere it appears — including inside nested tool-call JSON.

### 2. Run the cleaner

```bash
python vibe_clean.py --out cleaned_output
# options:
#   --agents opencode,claude-code     limit to specific agents
#   --min-score 70                    premium tier threshold
#   --blocklist path/to/blocklist.txt custom blocklist location
```

### 3. Review the output

```
cleaned_output/
├── agent_traces_premium.jsonl      completed trajectories >= min-score  ← submit this
├── agent_traces_unfiltered.jsonl   everything (kept for completeness)
├── review_sample.jsonl             20 random rows — READ THIS FIRST
└── quality_report.json             extraction/redaction/scoring stats
```

**Open `review_sample.jsonl` and confirm nothing identifying remains.**
The tool already does all of this automatically:

| What | Becomes |
|------|---------|
| Home paths (`/Users/you`, `/home/you`) | `~` |
| Project directory slugs (+ name variants) | `proj-<hash>` |
| Your blocklist terms | `[REDACTED]` |
| Emails | `[EMAIL]` |
| API keys / tokens (`sk-…`, `ghp_…`, bearer…) | `[REDACTED]` |
| Hostnames (`*.local`) | `[HOST]` |
| Session titles | dropped entirely |

## Output schema

One JSON object per line:

```jsonc
{
  "id": "opencode-ses_...-seg0",
  "agent": "opencode",
  "model": "deepseek-v4-flash-free",     // teacher model — filter per model downstream
  "split": "train",
  "messages": "[{role, content, reasoning_content?, tool_calls?}, {role:tool, content}, ...]",
  "project_id": "proj-1a2b3c4d",
  "quality_score": 95,
  "has_final": true,
  "error_loop": false,
  "err_rate": 0.05,
  "n_tool_calls": 12,
  "total_chars": 8421
}
```

Quality rubric (0-100): completed trajectory (+25), real tool work (+10), tool error rate (+15/+7), no repetition loops (+10), sane length (+5), refusal (-40), dead-end (-30).

## Publish to your own Hugging Face dataset

The publisher is generic — nothing is baked in for any particular user. Point it
at your own dataset repo:

```bash
pip install -r requirements.txt
hf auth login                              # a token with write access

cp blocklist.example.txt blocklist.txt     # your names/handles/project codenames
python vibe_clean.py --out cleaned_output  # extract + anonymize + score

python publish_hf.py --repo <you>/<dataset> \
    --input cleaned_output/agent_traces_unfiltered.jsonl
```

`publish_hf.py` creates the dataset (private by default) and writes the same
layout for everyone — `premium` / `standard` / `unfiltered` quality tiers plus a
config per source agent (`opencode`, `claude-code`, `grok-cli`,
`manicode-freebuff`) — and generates the dataset card. Re-run it whenever you
accumulate more sessions: new segments are appended and already-published ids
are replaced (upsert), so re-runs are safe.

Options: `--agents`, `--min-score`, `--baseline-rev`, `--public`, `--no-upload`
(write parquet locally without uploading), `--update-readme`.

## Submitting to this dataset

Prefer to contribute your traces to [the main dataset](https://huggingface.co/datasets/saidutta69/agentic-vibecoding-traces)?

Email **[contact@sdad.pro](mailto:contact@sdad.pro)** with your `agent_traces_premium.jsonl` (or a download link) after you've reviewed `review_sample.jsonl`. We'll run a second independent privacy check before anything is published to [the dataset](https://huggingface.co/datasets/saidutta69/agentic-vibecoding-traces).

## License

MIT — see [LICENSE](LICENSE).
