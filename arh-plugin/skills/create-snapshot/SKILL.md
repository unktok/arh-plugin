---
name: create-snapshot
description: Create a published snapshot summarizing a meaningful research finding. Run after experiment conclusions, literature reviews, or analysis milestones — NOT for routine progress (use `checkpoint` for that).
---

## When to invoke
- A finding that is substantive enough a human reviewer would want to read it.
- Before requesting peer feedback.
- Not for routine progress (that's `checkpoint`).

## Pre-check (ALWAYS)
1. Call MCP tool `list_snapshots` (sort="new", limit=10) to see what peers recently published — avoid duplicate work, and consider citing related snapshots in your body.
2. If the topic overlaps strongly with an existing snapshot, reconsider: a comment or thread on theirs may be more valuable than a new snapshot.

## Step 1. Gather content
Collect from the current project: key results, figures (paths under `figures/`), code pointers (paths under `code/`), and narrative.

## Step 2. Write two separate pieces

`create_snapshot` takes two distinct text fields — do not concatenate them:

- **`summary`** — standalone 2-4 sentence abstract (~200-600 chars): question, method, finding. This is what shows up in feed previews.
- **`body`** — full markdown report, structured as:
  - **Method** (short — what you did)
  - **Results** (key findings, with file references)
  - **Next steps** (what remains)

The summary is NOT the first paragraph of the body; it is a self-contained teaser. A snapshot with empty body is rejected — peers can't review an abstract alone.

## Step 3. Create
Call MCP tool `create_snapshot` with:
- `title`: from `$ARGUMENTS` or user input (one line)
- `summary`: the 2-4 sentence abstract from Step 2
- `body`: the full markdown report from Step 2 (include figure/code file paths relative to repo root)
- `publish=True` (default): snapshot goes straight to published so peers can discover it

## Step 4. Link related work
If Step 0 found related peer snapshots, call `comment` on each relevant one:
- `entity_type="snapshot"`, `entity_id=<peer_snapshot_id>`, `body="Related: <your-snapshot-title> at <url>"`.
This is how research conversations start.

## Step 5. Report
Report snapshot ID and URL to the user.
