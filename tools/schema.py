#!/usr/bin/env python3
"""
schema.py -- API tool schemas sent to the LLM.

Each entry defines a function that the model can call.
Adding a new tool requires an entry here plus a @_register implementation.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Tools available to sub-agent workers.
#
# When adding a new tool to TOOLS below, decide whether sub-agents should have
# access to it.  Sub-agents get a reduced tool set (no desktop/browser/MacOS
# automation, no MCP, no skills).  Add the tool name to this set if it should
# be available to sub-agents.
# ---------------------------------------------------------------------------
SUB_AGENT_TOOLS: set[str] = {
    # File & directory
    "read_file", "write_file", "edit_file", "list_directory", "file_info",
    "restore_file",
    # Search & navigation
    "search_files", "find_symbol", "find_usages", "semantic_search",
    # Shell & testing
    "run_shell", "run_tests", "verify",
    # Web
    "web_search", "fetch_url",
    # Agent coordination (blocked at max_depth by sub_agent.py runtime check)
    "spawn_agent", "agent_status", "collect_agent", "collect_any",
    "agent_extend", "agent_cancel", "agent_message", "agent_read",
    "agent_inbox", "agent_handoff", "agent_subscribe",
    # Scratchpad & planning
    "write_scratchpad", "todo_write", "todo_read",
    "plan", "plan_status",
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "todo_write",
            "description": "Create or update a todo item for tracking progress. Set content to empty string to delete. Use to track progress on complex multi-step tasks.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Optional: existing todo id to update. Omit to create new."},
                    "content": {"type": "string", "description": "Todo text. Set to empty string to delete this todo."},
                    "status": {"type": "string", "description": "Optional: 'pending' or 'done'. Default: 'pending'."}
                },
                "required": ["content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "todo_read",
            "description": "Read current todo list. Filter by id or status. Use this to check remaining work.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Optional: filter to a specific todo id."},
                    "status": {"type": "string", "description": "Optional: filter by 'pending' or 'done'."}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": "Capture a learning or observation to project_knowledge for cross-session persistence. Use when you discover a pattern, workaround, or convention worth remembering.",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "Short topic label for this learning (e.g. 'edit_file whitespace', 'module import pattern')"
                    },
                    "detail": {
                        "type": "string",
                        "description": "The learning itself -- what to remember, the pattern, workaround, or convention."
                    },
                    "category": {
                        "type": "string",
                        "description": "Optional: category hint (tool_usage, code_pattern, error_pattern, convention, architecture, workaround, dependency, general). Auto-detected if omitted."
                    }
                },
                "required": ["topic", "detail"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "memory_core",
            "description": "Manage your persistent core memory (frozen snapshot injected at session start). The agent's durable memory of facts, preferences, conventions, and environment notes. Changes persist to disk immediately but appear in the system prompt NEXT session. Use 'read' to see current snapshot, 'add' to append, 'replace' to rewrite entirely, 'remove' to delete by line number. Hard-capped at ~2,500 chars -- when full, consolidate (merge similar entries, remove stale ones) before adding. Example: memory_core(action='add', content='Python uses ruff for linting')",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "Action: 'read' (view current), 'add' (append entry), 'replace' (rewrite entire content), 'remove' (delete line by number)."
                    },
                    "content": {
                        "type": "string",
                        "description": "Content to add/replace. Required for 'add' and 'replace' actions."
                    },
                    "line": {
                        "type": "integer",
                        "description": "Line number to remove (1-indexed). Required for 'remove' action."
                    }
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_session_handoff",
            "description": "Write HANDOFF.md for session continuity. Auto-generates a summary of what changed this session using git diff. Call this before signing off to ensure the next session has context about what you worked on.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pending": {
                        "type": "string",
                        "description": "Optional: what's still pending / incomplete from this session."
                    },
                    "notes": {
                        "type": "string",
                        "description": "Optional: any additional notes for the next session."
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "find_symbol",
            "description": "Find where a Python symbol (function, class, method name) is defined in the workspace. Returns file path and line number for each match. Much faster than grep/search_files for symbol lookup. Supports substring matching.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Symbol name to find (e.g. '_request_with_retry', 'ToolResult'). Supports substring matching."
                    }
                },
                "required": [
                    "name"
                ]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file at the given path. Use offset and limit for line-range reads on large files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file to read"
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Optional: 0-indexed line number to start reading from (default: 0)."
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Optional: max lines to return (default: 300, absolute max: 1000)."
                    },
                    "line_numbers": {
                        "type": "boolean",
                        "description": "Optional: prefix each line with its line number (e.g. '42: content'). Default: false."
                    }
                },
                "required": [
                    "path"
                ]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file, overwriting if it exists.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file to write"
                    },
                    "content": {
                        "type": "string",
                        "description": "Content to write"
                    }
                },
                "required": [
                    "path",
                    "content"
                ]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Edit a file by replacing a specific string with another. Replaces first occurrence by default; use count=-1 for all. When preview=True, returns a unified diff without writing. Use 'paths' (list) for batch multi-file edits.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file to edit (required for single-file edit; ignored if 'paths' is provided)"
                    },
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional: list of file paths to apply the same old->new edit to (batch edit). When set, 'path' is ignored."
                    },
                    "old_string": {
                        "type": "string",
                        "description": "Exact string to find and replace"
                    },
                    "new_string": {
                        "type": "string",
                        "description": "String to replace it with"
                    },
                    "count": {
                        "type": "integer",
                        "description": "Optional: number of occurrences to replace (1 = first only, -1 = all). Default: 1."
                    },
                    "preview": {
                        "type": "boolean",
                        "description": "Optional: if true, skip the write and return a unified diff (lines starting with - for old, + for new). Default: false."
                    }
                },
                "required": [
                    "path",
                    "old_string",
                    "new_string"
                ]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List the contents of a directory at the given path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the directory to list"
                    }
                },
                "required": [
                    "path"
                ]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "Run a shell command inside the workspace directory. Returns exit code, stdout, and stderr. Timeout defaults to 60s, max 300s. Use for tests, syntax checks, build tools, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Shell command to execute (e.g. 'python -m pytest test_safety.py -v')"
                    },
                    "background": {
                        "type": "boolean",
                        "description": "Run in background, return immediately with task ID. Use task_status to check."
                    },
                    "force": {
                        "type": "boolean",
                        "description": "Bypass the destructive-command guard. Default: false. Required for rm, mkfs, etc."
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Optional: max seconds before timing out (default 60, max 300)."
                    },
                    "stdin": {
                        "type": "string",
                        "description": "Optional: string to pipe to the process's standard input."
                    }
                },
                "required": [
                    "command"
                ]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Search for a text pattern recursively in files within the workspace. Returns matching lines with file path and line number. Skips hidden directories, binary files, and common VCS/venv dirs. Capped at 200 results.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Text or substring to search for (case-sensitive by default). If regex is true, treated as a Python regex."
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory to search in (defaults to workspace root)"
                    },
                    "regex": {
                        "type": "boolean",
                        "description": "If true, treat pattern as a Python regex. Default: false."
                    },
                    "ignore_case": {
                        "type": "boolean",
                        "description": "If true, case-insensitive search. Default: false."
                    },
                    "file_path": {
                        "type": "string",
                        "description": "Optional: restrict search to a single file instead of a directory tree. When set, 'path' is ignored."
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Optional: skip the first N matching results (for pagination). Default: 0."
                    }
                },
                "required": [
                    "pattern"
                ]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "file_info",
            "description": "Get metadata about a file or directory at the given path. Returns size, permissions, modification time, type (file/directory), and whether the path exists. For directories also reports child count and total child size.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file or directory to inspect"
                    }
                },
                "required": [
                    "path"
                ]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": "Run tests in the workspace. Returns structured pass/fail counts and failure details. If 'path' is given, runs only those tests; otherwise runs all. Use background=True to run tests asynchronously and poll with task_status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Optional: specific test file or directory to run (e.g. 'test_tools.py' or 'test_memory.py'). If omitted, runs all tests."
                    },
                    "background": {
                        "type": "boolean",
                        "description": "If true, run tests in background and return a task_id immediately. Use task_status to poll for completion."
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Max seconds before timing out (default 120). Only applies in foreground mode."
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "semantic_search",
            "description": "Search code by meaning using embeddings. Finds code chunks semantically similar to the query, even if they don't share keywords. Good for finding related functionality, similar patterns, or code that 'feels like' something. Indexes files live -- no pre-indexing needed. Returns top 10 matches.\n\nWARNING: PERFORMANCE NOTE: The embedding model is preloaded at session startup in a background thread (~9s, ~80MB RAM) so it's typically ready before you need it. If you call semantic_search very early in a session you may see a brief \"still loading\" message while the background thread finishes. Still, prefer find_symbol (instant, indexed) or search_files (instant, grep) for exact name/text queries. Use semantic_search only when you don't know the function/variable name and grep won't work -- e.g. 'find code that validates user input' or 'locate retry logic patterns'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language description of what to find (e.g. 'error handling around file writes', 'retry logic')"
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory to search in (defaults to workspace root)"
                    }
                },
                "required": [
                    "query"
                ]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web using Exa. Returns relevant pages with titles, URLs, and highlighted excerpts. Good for documentation lookup, API references, current information, and technical questions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query. Be specific and use technical terms for best results."
                    },
                    "num_results": {
                        "type": "integer",
                        "description": "Number of results to return (default 5, max 20)."
                    },
                    "search_type": {
                        "type": "string",
                        "description": "Search depth: 'auto' (default, balanced), 'fast', 'deep'. 'auto' works for most queries."
                    }
                },
                "required": [
                    "query"
                ]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "task_status",
            "description": "Check the status of a background shell task by its ID. background=True in run_shell returns a task_id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Task ID returned by run_shell with background=True"
                    }
                },
                "required": [
                    "task_id"
                ]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_scratchpad",
            "description": "Write content to the agent's scratchpad -- a persistent working note that survives across turns. Tracks plan, progress, decisions, things tried, and open questions. Shown at start of each turn. Overwrites previous content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "Content to write to the scratchpad. Use markdown."
                    }
                },
                "required": [
                    "content"
                ]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "find_usages",
            "description": "Find all usages (references) of a Python symbol across the workspace. Returns file path, line number, and surrounding context for each usage. Much faster than grep for symbol references. Use this to find all callers of a function or all places a class/variable is used before refactoring.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Symbol name to find usages of (e.g. 'execute_tool', 'ToolResult')."
                    }
                },
                "required": [
                    "name"
                ]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "verify",
            "description": "Run lint + relevant tests for files modified in the current session. Uses tracked writes/edits to find matching test files. Falls back to running all tests if nothing has been modified yet. Use after code changes to verify nothing broke before moving on.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "restore_file",
            "description": "Restore a file from its session backup. Undoes the last write_file or edit_file operation on the given path. Only files modified in the current session can be restored.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file to restore from backup"
                    }
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "recall_turn",
            "description": "Recall a summary of what happened on a previous turn. Use this to recover lost context when old tool results have been pruned from the conversation. Returns tool calls made and their results for the given turn number.",
            "parameters": {
                "type": "object",
                "properties": {
                    "turn": {
                        "type": "integer",
                        "description": "Turn number to recall (1-indexed)"
                    }
                },
                "required": ["turn"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "session_search",
            "description": "Full-text search across all past session messages. Use when the user references something from a previous conversation ('we fixed this before', 'use the approach from last time'). Returns matching message excerpts ordered by relevance. Uses FTS5 full-text indexing for fast retrieval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search terms to find in past messages."
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results to return (default 10)."
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Fetch a web page URL and return its text content (truncated). Supports text/html and text/plain content types. Use this to read documentation, API references, or any web page.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL to fetch (must be http:// or https://)"
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Optional: request timeout in seconds (default 15, max 30)."
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Optional: max characters to return (default 10000)."
                    }
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "plan",
            "description": "Declare a structured task plan with numbered steps. Overwrites any previous plan. Use before multi-step work so progress is tracked. Shown at start of each turn until all steps complete.",
            "parameters": {
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Ordered list of step descriptions (e.g. ['Read config.py', 'Add new option', 'Update tests'])."
                    }
                },
                "required": ["steps"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "plan_status",
            "description": "Mark a plan step complete, or view current plan progress. No args: see plan and which steps are done. With 'step' (1-indexed): mark that step complete.",
            "parameters": {
                "type": "object",
                "properties": {
                    "step": {
                        "type": "integer",
                        "description": "Optional: 1-indexed step number to mark complete. Omit to just view current plan status."
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "spawn_agent",
            "description": "Spawn one or more sub-agents to work on tasks in background threads. Returns a task_id immediately -- the parent does NOT block. Use agent_status to poll or collect_agent to block later when you need the result. For multiple tasks, pass 'tasks' (list) instead of 'task' to spawn them all in one call. Sub-agents share your workspace and tools but have their own context. Max 10 concurrent sub-agents, 25 turns each (extendable to 35). Set 'synchronous'=true to block until completion and return the result directly (agent-as-tool pattern).",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Task description for the sub-agent. Be specific about what to do and what output you expect. Use this OR 'tasks' (not both)."
                    },
                    "tasks": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Multiple task descriptions to spawn in parallel. Use this OR 'task' (not both). Max 10 at a time."
                    },
                    "synchronous": {
                        "type": "boolean",
                        "description": "Optional: if true, block until the sub-agent(s) complete and return results directly (agent-as-tool pattern). Default: false."
                    },
                    "shared_context": {
                        "type": "string",
                        "description": "Optional: information shared with all spawned sub-agents (API contracts, schemas, coordination notes). Injected as a system message."
                    },
                    "max_turns": {
                        "type": "integer",
                        "description": "Optional: max turns per sub-agent (default 15, max 35)."
                    },
                    "visible": {
                        "type": "boolean",
                        "description": "If true, stream the sub-agent's thinking and tool output to stderr so the user can watch progress inline."
                    },
                    "subscriptions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional: list of message types this sub-agent subscribes to. Empty = all types (default)."
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "session_stats",
            "description": "Show session statistics: turns used, context tokens, active sub-agents, plan progress. No parameters needed.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "agent_status",
            "description": "Check the status of a sub-agent without blocking. Returns 'running', 'completed' with a result summary, or 'not_found'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Task ID returned by spawn_agent."
                    }
                },
                "required": ["task_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "collect_agent",
            "description": "Block until a sub-agent completes (or times out at 30s), then return its full result. Use this when you're ready to consume the sub-agent's output.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Task ID returned by spawn_agent."
                    }
                },
                "required": ["task_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "collect_any",
            "description": "Collect the first sub-agent that finishes (from a list of task_ids). If any have already completed, returns immediately. Otherwise polls until one completes or timeout (60s). Use after spawn_agent with multiple tasks to grab the fastest result.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of task IDs to check. If omitted, checks all known sub-agents."
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "agent_message",
            "description": "Broadcast a message visible to the parent and all sibling sub-agents. Use to share API schemas, status updates, or results that other agents need.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Message text to broadcast."
                    },
                    "from": {
                        "type": "string",
                        "description": "Optional label identifying the sender (e.g. 'backend-agent')."
                    }
                },
                "required": ["text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "agent_read",
            "description": "Read broadcast messages from other sub-agents and the parent. Returns messages in chronological order.",
            "parameters": {
                "type": "object",
                "properties": {
                    "since": {
                        "type": "integer",
                        "description": "Optional: only return messages with index >= this value (for polling)."
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "agent_extend",
            "description": "Extend the turn budget of a running sub-agent. Use when a sub-agent is still making progress but needs more turns to finish. Check agent_status first to confirm it's still running.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Task ID returned by spawn_agent."
                    },
                    "additional": {
                        "type": "integer",
                        "description": "Additional turns to grant (default 10)."
                    }
                },
                "required": ["task_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "agent_cancel",
            "description": "Cancel a running sub-agent by sending a cancellation signal. The sub-agent will stop at its next turn boundary. Use agent_status to confirm cancellation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Task ID returned by spawn_agent."
                    }
                },
                "required": ["task_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "agent_handoff",
            "description": "Produce a typed structured result and route it to subscribed agents. Use this for handoffs between agents -- one agent finishes work and hands structured output to another. If 'target' is set, delivers only to that task_id (bypassing subscriptions).",
            "parameters": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "description": "Message type (default 'handoff.result'). Use 'handoff.result', 'handoff.request', 'handoff.ack', 'status.heartbeat', 'status.error', 'coord.fan_out', 'coord.fan_in', or 'coord.sync'."
                    },
                    "result": {
                        "type": "object",
                        "description": "Structured result payload dict."
                    },
                    "correlation_id": {
                        "type": "string",
                        "description": "Optional correlation ID to link related messages."
                    },
                    "target": {
                        "type": "string",
                        "description": "Optional: if set, deliver only to this task_id."
                    },
                    "from": {
                        "type": "string",
                        "description": "Optional label identifying the sender."
                    }
                },
                "required": ["result"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "agent_inbox",
            "description": "Read the typed inbox for a specific agent (task_id). Returns structured messages in chronological order. Use 'since' for polling new messages only.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Task ID of the agent whose inbox to read. If omitted, defaults to your own inbox."
                    },
                    "since": {
                        "type": "integer",
                        "description": "Optional: only return messages with index >= this value (for polling)."
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "agent_subscribe",
            "description": "Declare or update message type subscriptions for an agent at runtime. An empty types list means the agent receives all message types.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Task ID of the agent to configure."
                    },
                    "types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Message types to subscribe to (e.g. ['handoff.result', 'coord.sync']). Omit to reset to receive all types."
                    }
                },
                "required": ["task_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "lsp_definition",
            "description": "Go to definition using the Language Server Protocol. Given a file path and a position (line, character), returns the location(s) where the symbol is defined. Requires pylsp for Python or typescript-language-server for JS/TS.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the file to query."
                    },
                    "line": {
                        "type": "integer",
                        "description": "0-indexed line number of the symbol."
                    },
                    "character": {
                        "type": "integer",
                        "description": "0-indexed character offset within the line."
                    }
                },
                "required": ["file_path", "line", "character"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "lsp_references",
            "description": "Find all references to a symbol using the Language Server Protocol. Given a file path and position, returns all locations that reference the symbol. Requires pylsp for Python or typescript-language-server for JS/TS.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the file to query."
                    },
                    "line": {
                        "type": "integer",
                        "description": "0-indexed line number of the symbol."
                    },
                    "character": {
                        "type": "integer",
                        "description": "0-indexed character offset within the line."
                    },
                    "include_declaration": {
                        "type": "boolean",
                        "description": "Whether to include the declaration itself in results. Default: true."
                    }
                },
                "required": ["file_path", "line", "character"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "lsp_hover",
            "description": "Get hover information (type, docs, signature) for a symbol using the Language Server Protocol. Given a file path and position, returns documentation for the symbol at that location. Requires pylsp for Python or typescript-language-server for JS/TS.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the file to query."
                    },
                    "line": {
                        "type": "integer",
                        "description": "0-indexed line number of the symbol."
                    },
                    "character": {
                        "type": "integer",
                        "description": "0-indexed character offset within the line."
                    }
                },
                "required": ["file_path", "line", "character"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "lsp_diagnostics",
            "description": "Get diagnostics (errors, warnings, hints) for a file using the Language Server Protocol. Opens the document and collects published diagnostics. Requires pylsp for Python or typescript-language-server for JS/TS.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the file to check for diagnostics."
                    }
                },
                "required": ["file_path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "fan_out",
            "description": "Fan-out: spawn multiple sub-agents from a list of task descriptions. Each description becomes a separate sub-agent. Use this to parallelize independent work across multiple agents in one call. Returns a list of task IDs. Sub-agents share your workspace and tools.",
            "parameters": {
                "type": "object",
                "properties": {
                    "descriptions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of task descriptions, one per sub-agent. Each description should be specific about what to do and what output is expected."
                    },
                    "shared_context": {
                        "type": "string",
                        "description": "Optional: information shared with all spawned sub-agents (API contracts, schemas, coordination notes). Injected as a system message."
                    },
                    "max_turns": {
                        "type": "integer",
                        "description": "Optional: max turns per sub-agent (default 15, max 35)."
                    },
                    "subscriptions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional: list of message types each sub-agent subscribes to. Empty = all types (default)."
                    }
                },
                "required": ["descriptions"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "fan_in",
            "description": "Fan-in: collect results from a set of previously spawned sub-agents. Blocks until all complete or timeout elapses. Use after fan_out to gather results from parallel work.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of task IDs to collect results from."
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Optional: max seconds to wait for all sub-agents to complete (default 60, max 300)."
                    }
                },
                "required": ["task_ids"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "pipeline",
            "description": "Pipeline: execute a sequence of tasks in order, where each stage runs only after the previous one completes. Each stage is a task description that runs as a sub-agent. Results from earlier stages are available to later stages via shared context.",
            "parameters": {
                "type": "object",
                "properties": {
                    "stages": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Ordered list of task descriptions, executed sequentially. Each stage waits for the previous one to finish before starting."
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Optional: max seconds total for the entire pipeline (default 300, max 600)."
                    }
                },
                "required": ["stages"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "barrier",
            "description": "Barrier: block until all specified sub-agents have completed. Use this to synchronize parallel agents before proceeding to a next phase. Returns completion status for each task ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Human-readable name for this barrier (e.g. 'phase-1-complete'). Used in logs and error messages."
                    },
                    "task_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of task IDs to wait for. Blocks until all have completed."
                    }
                },
                "required": ["name", "task_ids"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "audit_parallel",
            "description": "Fan-out a code audit across multiple defect classes in parallel. Each defect class gets its own sub-agent. Use this to run comprehensive code audits (bugs, performance, style, etc.) concurrently. Returns task IDs; collect results with collect_any/collect_agent.",
            "parameters": {
                "type": "object",
                "properties": {
                    "defect_classes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of defect categories to audit (e.g. ['bugs & error handling', 'code quality', 'performance'])."
                    },
                    "workspace_scope": {
                        "type": "string",
                        "description": "Optional: limit audit to a subdirectory (e.g. 'tools/')."
                    },
                    "max_turns": {
                        "type": "integer",
                        "description": "Optional: turn budget per auditor (default 15). Lower values enforce faster audits."
                    }
                },
                "required": ["defect_classes"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "scatter_gather",
            "description": "Scatter-gather: apply a single task template across a list of items in parallel, then collect results. Each item is substituted into the template to create a sub-agent task. Use this to process a list of items with the same logic concurrently.",
            "parameters": {
                "type": "object",
                "properties": {
                    "template": {
                        "type": "string",
                        "description": "Task description template with {item} placeholder. Each {item} is replaced with one entry from the items list."
                    },
                    "items": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of items to scatter. Each creates a sub-agent with the template description."
                    },
                    "max_turns": {
                        "type": "integer",
                        "description": "Optional: max turns per sub-agent (default 15, max 35)."
                    }
                },
                "required": ["template", "items"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_image",
            "description": "Read an image file, send it to GPT-4o, and return a text description of what the model sees. Use this to understand images, screenshots, diagrams, or photos.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the image file to describe."
                    }
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "wait_for_agent",
            "description": "Block until any sub-agent from the given list completes, or timeout expires. Uses exponential backoff sleep (1s->2s->4s...->30s) to minimize token burn while waiting. Returns immediately if any agent has already completed. Use this instead of repeated collect_any calls to save on LLM cost.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of sub-agent task IDs to wait for."
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Max seconds to wait (default 120)."
                    }
                },
                "required": ["task_ids"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "diagnose_failures",
            "description": "Read the last test run output from memory store, parse for FAILED lines, extract test function names and file paths, read the relevant source files, and return a structured failure summary with code snippets. No parameters needed -- reads automatically from the persisted test output.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },

    {
        "type": "function",
        "function": {
            "name": "init",
            "description": "Analyze the workspace and auto-generate .mini_agent.rules (coding conventions, module map) and .mini_agent.toml (if missing). Also seed project_knowledge with auto-detected learnings about the codebase structure. Use this on first run or when the project structure has changed significantly.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "open_url",
            "description": "Open the user's default browser to the given URL. Opens in a new tab and returns immediately -- does not wait for the page to load. For programmatic browser interaction, use the browser_* tools instead.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL to open. Must start with http:// or https://."
                    }
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browser_navigate",
            "description": "Navigate a headless browser (Playwright Chromium) to a URL. Returns the page title and final URL after redirects. Requires playwright to be installed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL to navigate to. Must start with http:// or https://."
                    }
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browser_snapshot",
            "description": "Capture the accessibility tree of the current browser page. Returns a structured text representation of interactive elements (roles, names, states) -- much more compact and LLM-friendly than raw HTML or a screenshot. Use this to understand what's on the page before clicking or typing.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browser_click",
            "description": "Click an element on the current browser page identified by its accessibility role and name. Use browser_snapshot first to see available elements.",
            "parameters": {
                "type": "object",
                "properties": {
                    "role": {
                        "type": "string",
                        "description": "ARIA role of the element (e.g. 'button', 'link', 'textbox', 'checkbox')"
                    },
                    "name": {
                        "type": "string",
                        "description": "Accessible name of the element (visible text or aria-label)"
                    }
                },
                "required": ["role", "name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browser_type",
            "description": "Type text into an input element on the current browser page identified by its role and name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "role": {
                        "type": "string",
                        "description": "ARIA role (typically 'textbox' or 'searchbox')"
                    },
                    "name": {
                        "type": "string",
                        "description": "Accessible name (label text, placeholder, or aria-label)"
                    },
                    "text": {
                        "type": "string",
                        "description": "Text to type into the element"
                    }
                },
                "required": ["name", "text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browser_screenshot",
            "description": "Capture a full-page PNG screenshot of the current browser page. Saves to the workspace so it can be inspected with read_image.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path within workspace to save the screenshot (default: browser_screenshot.png)"
                    },
                    "full_page": {
                        "type": "boolean",
                        "description": "Capture the full scrollable page (default: true)"
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "mcp_discover",
            "description": "List all tools from all connected MCP (Model Context Protocol) servers. Use this to see what external tools are available before calling them with mcp_call.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "mcp_call",
            "description": "Call a tool on a specific MCP (Model Context Protocol) server. Use mcp_discover first to see available servers and tools.",
            "parameters": {
                "type": "object",
                "properties": {
                    "server": {
                        "type": "string",
                        "description": "MCP server name (e.g. 'my-server'). Use mcp_discover to see available servers."
                    },
                    "tool": {
                        "type": "string",
                        "description": "Tool name to call on the server (e.g. 'calculate', 'get_weather')."
                    },
                    "arguments": {
                        "type": "object",
                        "description": "Optional: keyword arguments to pass to the MCP tool."
                    }
                },
                "required": ["server", "tool"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_snapshot",
            "description": "Capture the accessibility tree of the frontmost desktop window. Returns a structured text representation of interactive elements (roles, names, states) -- much more compact and LLM-friendly than a screenshot. Use this to understand what's on screen before clicking or typing in native desktop apps. On macOS, requires Accessibility permission (System Settings -> Privacy -> Accessibility -> enable Terminal). On Windows, requires: pip install uiautomation.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_click",
            "description": "Click a native desktop UI element identified by its role and name. Use desktop_snapshot first to see available elements. Supports macOS (via Accessibility API) and Windows (via UI Automation). Args: role (e.g. 'button', 'textfield', 'checkbox', 'menuItem'), name (visible text or label).",
            "parameters": {
                "type": "object",
                "properties": {
                    "role": {
                        "type": "string",
                        "description": "Element role (e.g. 'button', 'textfield', 'checkbox', 'menuItem', 'tab', 'link', 'window'). See desktop_snapshot output for available roles."
                    },
                    "name": {
                        "type": "string",
                        "description": "Accessible name of the element (visible text, label, or aria-label equivalent)."
                    }
                },
                "required": ["role", "name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_type",
            "description": "Type text into the currently focused native desktop field. Click into the target field first (using desktop_click or manually), then call this to type. Args: text (string to type).",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Text to type into the focused field."
                    }
                },
                "required": ["text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_find",
            "description": "Find native desktop UI elements matching a text or role query across all open windows. Args: query (text to search for in element names/labels), role (optional role filter like 'button', 'window', 'menu').",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Text to search for in element names/labels. Case-insensitive partial match."
                    },
                    "role": {
                        "type": "string",
                        "description": "Optional: filter by role (e.g. 'button', 'window', 'menu'). Omit to search all roles."
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_screenshot",
            "description": "Capture a PNG screenshot of the native desktop (not browser). Unlike browser_screenshot, this captures any open application, menubar, dock, taskbar, etc. Saves to a temp directory. Use read_image to view it. Requires: pip install mss.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_apps",
            "description": "List all running desktop applications with PID, name, bundle ID, and foreground status. Use this to see what's running before launching, quitting, or focusing apps.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_launch",
            "description": "Launch a macOS application by name (e.g. 'Safari', 'Terminal', 'Visual Studio Code') or bundle ID (e.g. 'com.apple.Safari'). Uses 'open -a' under the hood.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Application name (e.g. 'Safari', 'Finder', 'Terminal') or bundle ID (e.g. 'com.apple.mail')."
                    }
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_quit",
            "description": "Quit a macOS application by name (e.g. 'Safari') or PID. Tries gentle quit first (osascript), then falls back to pkill for force-quit.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Application name (e.g. 'Safari', 'Terminal') or PID string."
                    }
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_focus",
            "description": "Bring a macOS application window to the foreground (activate it). Use after desktop_launch or to switch between running apps.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Application name to activate (e.g. 'Safari', 'Terminal')."
                    }
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_clipboard",
            "description": "Read from or write to the macOS system clipboard. Use action='read' to get current clipboard content, or action='write' with the text parameter to set it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "'read' to get clipboard contents, 'write' to set clipboard to the 'text' parameter."
                    },
                    "text": {
                        "type": "string",
                        "description": "Text to copy to clipboard (only when action='write')."
                    }
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_windows",
            "description": "List all visible windows across all macOS applications using CGWindowList. Shows window titles, owner apps, sizes, positions, and PIDs. Much more comprehensive than desktop_snapshot which only covers the frontmost app.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_system_info",
            "description": "Gather macOS system metrics: hostname, OS version, CPU cores, physical memory, disk usage, battery status, thermal state, load average, and system uptime. All in one call.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_key",
            "description": "Press a macOS key combination via CGEvent (or AppleScript fallback). Examples: 'cmd+c' (copy), 'cmd+v' (paste), 'cmd+tab' (app switcher), 'cmd+shift+4' (screenshot region), 'escape', 'return', 'space', 'left', 'right', 'f5', 'f11'. Supports modifiers: cmd, shift, option/alt, ctrl.",
            "parameters": {
                "type": "object",
                "properties": {
                    "combo": {
                        "type": "string",
                        "description": "Key combination string (e.g. 'cmd+c', 'cmd+shift+4', 'cmd+tab', 'escape', 'return', 'space', 'left', 'right', 'up', 'down', 'f5', 'f11'). Modifiers: cmd, shift, option/alt, ctrl, fn."
                    }
                },
                "required": ["combo"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_open",
            "description": "Open a file, folder, or URL in the default macOS application. Files open with their associated app, folders open in Finder, URLs open in the default browser. Equivalent to the 'open' command.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "File path, folder path, or URL to open (e.g. '/Users/me/doc.pdf', '/Applications', 'https://github.com')."
                    }
                },
                "required": ["target"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_reveal",
            "description": "Reveal a file or folder in Finder (opens a Finder window with the item selected). Use this to show the user where a file is located.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path to the file or folder to reveal in Finder."
                    }
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_notify",
            "description": "Post a macOS system notification (appears as a banner in Notification Center). Useful for alerting the user when a long-running task completes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Notification title (required)."
                    },
                    "message": {
                        "type": "string",
                        "description": "Notification body text (optional)."
                    },
                    "sound": {
                        "type": "boolean",
                        "description": "Play default notification sound. Default: false."
                    }
                },
                "required": ["title"]
            }
        }
    },
]
