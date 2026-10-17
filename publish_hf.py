#!/usr/bin/env python3
"""
publish_hf.py — publish the output of vibe_clean.py to a Hugging Face dataset.

Nothing here is specific to any one user: point it at *your* dataset repo and
*your* cleaned output. Run it once to create the dataset, and re-run it whenever
you accumulate more sessions — new segments are appended and any row with an
already-published id is replaced (upsert), so re-runs are safe.

It writes these configs (same layout for everyone):

  premium      default — completed, deduped, scored >= --min-score
  standard     scored >= 50
  unfiltered   everything (RL / negative mining)
  <agent>      premium-tier rows for each source agent (e.g. `opencode`)

Usage
  python publish_hf.py --repo <you>/<dataset> \
      --input cleaned_output/agent_traces_unfiltered.jsonl

  # options
  #   --agents opencode,claude-code   limit to some agents
  #   --min-score 70                  premium tier threshold
  #   --baseline-rev <hf revision>    diff against a specific past revision
  #   --public                        create the repo public (default: private)
  #   --no-upload                     write parquet locally under --out only
  #   --update-readme                 regenerate the dataset card

Requires: pip install -r requirements.txt  and a logged-in `hf auth login`
(the token needs write access to the target repo).
"""
from __future__ import annotations

import argparse, hashlib, json, re, sys
from collections import Counter, defaultdict
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from huggingface_hub import HfApi, hf_hub_download

EMAIL = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
SECRET = re.compile(
    r"(sk-[A-Za-z0-9_\-]{16,}|(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|AIza[0-9A-Za-z_\-]{35}|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|glpat-[0-9A-Za-z_\-]{20,}|npm_[A-Za-z0-9]{36}|SG\.[A-Za-z0-9_\-]{22}\.[A-Za-z0-9_\-]{43}"
    r"|(?:sk|rk)_live_[0-9a-zA-Z]{24,}|hf_[A-Za-z0-9]{30,}|Bearer\s+[A-Za-z0-9._\-]{20,})"
)
USERPATH = re.compile(r"/(?:Users|home)/")

SPLITS = ["train", "validation", "test"]
TIERS = ["premium", "standard", "unfiltered"]
STANDARD_MIN = 50
SCHEMA = pa.schema([
    ("id", pa.string()), ("agent", pa.string()), ("model", pa.string()),
    ("split", pa.string()), ("project_id", pa.string()),
    ("n_tool_calls", pa.int64()), ("n_assistant_turns", pa.int64()),
    ("user_request_chars", pa.int64()), ("total_chars", pa.int64()),
    ("messages", pa.string()), ("quality_score", pa.int64()),
    ("has_final", pa.bool_()), ("refused", pa.bool_()), ("error_loop", pa.bool_()),
    ("err_rate", pa.float64()), ("n_tools", pa.int64()), ("final_text_len", pa.int64()),
])
FIELDS = [f.name for f in SCHEMA]


def split_of(key: str) -> str:
    x = int(hashlib.md5(str(key).encode()).hexdigest(), 16) % 1000
    return "train" if x < 900 else ("validation" if x < 950 else "test")


def normalize(row: dict) -> dict:
    """Coerce a vibe_clean row into the published schema, recomputing every
    derived field from `messages` so the output is self-consistent."""
    msgs = row["messages"]
    if isinstance(msgs, str):
        msgs = json.loads(msgs)
    asst = [m for m in msgs if m.get("role") == "assistant"]
    n_tc = sum(len(m.get("tool_calls") or []) for m in msgs)
    n_tools = sum(1 for m in msgs if m.get("role") == "tool")
    usr = next((m for m in msgs if m.get("role") == "user"), None)
    uq = str(usr.get("content", "")) if usr else ""
    total = sum(len(str(m.get("content", ""))) + len(str(m.get("reasoning_content", "") or "")) for m in msgs)
    ft = ""
    for m in reversed(asst):
        c = str(m.get("content", "")).strip()
        if c:
            ft = c
            break
    return {
        "id": row["id"], "agent": row["agent"], "model": row.get("model", "unknown"),
        "split": split_of(row["id"]),
        "project_id": row.get("project_id", "proj-" + hashlib.sha1(b"unknown").hexdigest()[:8]),
        "n_tool_calls": n_tc, "n_assistant_turns": len(asst), "user_request_chars": len(uq),
        "total_chars": total, "messages": json.dumps(msgs, ensure_ascii=False),
        "quality_score": int(row.get("quality_score", 0)),
        "has_final": bool(row.get("has_final", len(ft) > 80)),
        "refused": bool(row.get("refused", False)),
        "error_loop": bool(row.get("error_loop", False)),
        "err_rate": float(row.get("err_rate", 0.0)),
        "n_tools": n_tools, "final_text_len": len(ft),
    }


def is_premium(r: dict, min_score: int) -> bool:
    return r["quality_score"] >= min_score and r["has_final"] and not r["refused"] and not r["error_loop"]


def load_blocklist(path: str) -> list[str]:
    p = Path(path)
    if not p.exists():
        return []
    return [l.strip() for l in p.read_text().splitlines() if l.strip() and not l.startswith("#")]


def leak_scan(rows: list[dict], blocklist: list[str]) -> Counter:
    """Independent pre-upload check: emails, secret prefixes, home/user paths,
    and your blocklist terms. vibe_clean.py should have removed all of these."""
    home = str(Path.home())
    pats = {
        "email": EMAIL, "secret": SECRET, "userpath": USERPATH,
        "home": re.compile(re.escape(home)),
        "username": re.compile(re.escape(Path.home().name), re.IGNORECASE),
    }
    bl = re.compile("(" + "|".join(re.escape(t.lower()) for t in blocklist) + ")", re.IGNORECASE) if blocklist else None
    hits = Counter()
    for r in rows:
        for m in json.loads(r["messages"]):
            for f in ("content", "reasoning_content"):
                s = str(m.get(f, "") or "")
                for k, p in pats.items():
                    n = len(p.findall(s))
                    if n:
                        hits[k] += n
                if bl is not None:
                    n = len(bl.findall(s))
                    if n:
                        hits["blocklist"] += n
    return hits


def include(cfg: str, r: dict, agents: set[str], min_score: int) -> bool:
    if cfg == "unfiltered":
        return True
    if cfg == "standard":
        return r["quality_score"] >= STANDARD_MIN
    if cfg == "premium":
        return is_premium(r, min_score)
    return r["agent"] == cfg and is_premium(r, min_score)


def load_existing(repo: str, cfg: str, split: str, rev):
    try:
        p = hf_hub_download(repo, f"data/{cfg}/{split}.parquet", repo_type="dataset", revision=rev)
        t = pq.read_table(p)
        return [{f: row.get(f) for f in FIELDS} for row in t.to_pylist()]
    except Exception:
        return []


def make_readme(repo: str, cfgs: list[str], counts: dict, models: Counter, agents: Counter, written: dict) -> str:
    lines = [
        "---",
        "license: mit",
        "task_categories:",
        "  - text-generation",
        "tags:",
        "  - agentic",
        "  - agent-traces",
        "  - tool-use",
        "  - sft",
        "  - distillation",
        "  - anonymized",
        "configs:",
    ]
    for cfg in cfgs:
        if cfg not in written:
            continue
        lines.append(f"  - config_name: {cfg}")
        if cfg == "premium":
            lines.append("    default: true")
        lines.append("    data_files:")
        for split in written[cfg]:
            lines += [f"      - split: {split}", f"        path: data/{cfg}/{split}.parquet"]
    lines += [
        "---",
        "",
        f"# {repo.split('/')[-1].replace('-', ' ').title()}",
        "",
        "Anonymized multi-agent coding traces, segmented per task and scored for quality.",
        "",
        "| Config | Rows |",
        "|--------|-----:|",
    ]
    for cfg in cfgs:
        if cfg in written:
            lines.append(f"| `{cfg}` | {counts.get(cfg, 0):,} |")
    lines += ["", "## Source agents", "", "| Agent | Rows |", "|-------|-----:|"]
    for a, c in agents.most_common():
        lines.append(f"| {a} | {c:,} |")
    lines += ["", "## Teacher models", "", f"{len(models)} distinct `model` values. Top:", ""]
    lines += ["| Model | Rows |", "|-------|-----:|"]
    for m, c in models.most_common(15):
        lines.append(f"| {m} | {c:,} |")
    lines += [
        "",
        "## Fields",
        "",
        "`id`, `agent`, `model`, `split`, `project_id`, `n_tool_calls`, `n_assistant_turns`,",
        "`user_request_chars`, `total_chars`, `messages` (JSON: OpenAI-format with `tool_calls`",
        "and `reasoning_content`), `quality_score`, `has_final`, `refused`, `error_loop`,",
        "`err_rate`, `n_tools`, `final_text_len`.",
        "",
        "## Usage",
        "",
        "```python",
        "from datasets import load_dataset",
        f'ds = load_dataset("{repo}", "premium", split="train")',
        "```",
        "",
        "Generated by [vibe-trace-cleaner](https://github.com/instax-dutta/vibe-trace-cleaner).",
        "",
    ]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Publish cleaned agent traces to a Hugging Face dataset.")
    ap.add_argument("--repo", required=True, help="target dataset repo id, e.g. <you>/<dataset>")
    ap.add_argument("--input", default="cleaned_output/agent_traces_unfiltered.jsonl",
                    help="cleaned jsonl from vibe_clean.py")
    ap.add_argument("--agents", default="all", help="comma list to limit agents (default all)")
    ap.add_argument("--min-score", type=int, default=70, help="premium threshold (default 70)")
    ap.add_argument("--baseline-rev", default=None, help="HF revision to diff against (default: latest)")
    ap.add_argument("--out", default="cleaned_output/publish", help="local parquet output dir")
    ap.add_argument("--public", action="store_true", help="create the repo public (default private)")
    ap.add_argument("--no-upload", action="store_true", help="write parquet locally, do not upload")
    ap.add_argument("--update-readme", action="store_true", help="(re)generate the dataset card")
    ap.add_argument("--blocklist", default="blocklist.txt", help="blocklist used by vibe_clean.py (for the pre-upload check)")
    ap.add_argument("--allow-leaks", action="store_true", help="upload even if the leak check finds something")
    args = ap.parse_args()

    if not Path(args.input).exists():
        sys.exit(f"input not found: {args.input} (run vibe_clean.py first)")
    rows = [normalize(json.loads(l)) for l in open(args.input) if l.strip()]
    if args.agents != "all":
        allow = set(args.agents.split(","))
        rows = [r for r in rows if r["agent"] in allow]
    seen = set()
    rows = [r for r in rows if not (r["id"] in seen or seen.add(r["id"]))]
    if not rows:
        sys.exit("no rows to publish")

    hits = leak_scan(rows, load_blocklist(args.blocklist))
    if hits:
        print("LEAK SCAN:", dict(hits), flush=True)
        if not args.allow_leaks:
            sys.exit("aborting: possible identifiers/secrets found (fix blocklist or pass --allow-leaks)")
    else:
        print("leak scan: clean", flush=True)

    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    agents = sorted({r["agent"] for r in rows})
    cfgs = TIERS + agents
    new_ids = {r["id"] for r in rows}

    api = HfApi()
    if not args.no_upload:
        api.create_repo(args.repo, repo_type="dataset", private=not args.public, exist_ok=True)
        print(f"target repo: {args.repo}", flush=True)

    counts = Counter()
    uploads = []
    written = defaultdict(list)
    for cfg in cfgs:
        for split in SPLITS:
            existing = load_existing(args.repo, cfg, split, args.baseline_rev) if not args.no_upload else []
            existing = [r for r in existing if r["id"] not in new_ids]  # upsert
            add = [r for r in rows if r["split"] == split and include(cfg, r, set(agents), args.min_score)]
            if not add and not existing:
                continue
            merged = existing + add
            tbl = pa.Table.from_pylist(merged, schema=SCHEMA)
            path = out / f"{cfg}_{split}.parquet"
            pq.write_table(tbl, path, compression="SNAPPY")
            counts[cfg] += tbl.num_rows
            written[cfg].append(split)
            dest = f"data/{cfg}/{split}.parquet"
            if not args.no_upload:
                api.upload_file(path_or_fileobj=str(path), path_in_repo=dest, repo_id=args.repo, repo_type="dataset")
            print(f"  {cfg}/{split}: +{len(add)} -> {tbl.num_rows}", flush=True)
            uploads.append(dest)

    models = Counter(r["model"] for r in rows)
    agent_counts = Counter(r["agent"] for r in rows)

    if not args.no_upload:
        # only write the card if the dataset does not already have a README, unless forced
        try:
            hf_hub_download(args.repo, "README.md", repo_type="dataset")
            have_readme = True
        except Exception:
            have_readme = False
        if args.update_readme or not have_readme:
            card = out / "README.md"
            card.write_text(make_readme(args.repo, cfgs, counts, models, agent_counts, written), encoding="utf-8")
            api.upload_file(path_or_fileobj=str(card), path_in_repo="README.md", repo_id=args.repo, repo_type="dataset")
            print("  README.md written", flush=True)

    print("\n=== SUMMARY ===")
    print("configs:", dict(counts))
    print("agents :", dict(agent_counts))
    print("models :", f"{len(models)} distinct")
    print("output :", out.resolve())
    if not args.no_upload:
        print(f"done -> https://huggingface.co/datasets/{args.repo}")


if __name__ == "__main__":
    main()
