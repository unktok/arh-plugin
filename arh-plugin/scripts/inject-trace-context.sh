#!/bin/bash
# SessionStart hook: inject ARH_TRACE_ID from .arh-trace file into the environment.
#
# When an orchestrator creates a trace context, it writes a .arh-trace file
# in the working directory. This hook reads it and exports ARH_TRACE_ID
# via CLAUDE_ENV_FILE so that all MCP tools and subsequent hooks can access it.
#
# This enables automatic trace context propagation to team agents that share
# the same working directory.

TRACE_FILE="$(pwd)/.arh-trace"
if [ -f "$TRACE_FILE" ] && [ -n "$CLAUDE_ENV_FILE" ]; then
  TRACE_ID=$(python3 -c "import json; print(json.load(open('$TRACE_FILE'))['trace_id'])" 2>/dev/null)
  if [ -n "$TRACE_ID" ]; then
    echo "export ARH_TRACE_ID='$TRACE_ID'" >> "$CLAUDE_ENV_FILE"
  fi
fi
exit 0
