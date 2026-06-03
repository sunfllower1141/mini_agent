#!/usr/bin/env python3
"""
extract_training_data.py — Extract GRPO training examples from mini_agent's
SQLite conversation memory.

Parses the messages table to find tool-call sequences:
  assistant → tool_call → tool_result

and formats them as training examples for grpo_train.py.

Usage:
    python extract_training_data.py [--db .mini_agent_memory.db] [--output training_data.jsonl]
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from typing import Any


# ---------------------------------------------------------------------------
# Tool call patterns from mini_agent's API response format
# ---------------------------------------------------------------------------

# mini_agent sends tool calls as XML-ish blocks: <function=name>args</function>
TOOL_CALL_PATTERN = r'<function=(\w+)>(.*?)</function>'

# Known tool names for validation
KNOWN_TOOLS = {
    "read_file", "write_file", "edit_file", "list_directory", "file_info",
    "write_scratchpad", "run_shell", "run_tests", "search_files", "find_symbol",
    "find_usages", "web_search", "fetch_url", "remember", "plan", "plan_status",
    "task_status", "diff", "restore_file", "verify", "diagnose_failures",
    "spawn_agent", "agent_status", "collect_agent", "agent_message",
    "git", "use_skill", "session_stats", "recall_turn",
    "lsp_definition", "lsp_references", "lsp_hover", "lsp_diagnostics",
    "mcp_discover", "mcp_call",
}


@dataclass
class ToolCallExample:
    """A single tool-call example extracted from conversation history."""
    prompt: str              # Messages leading up to the tool call
    completion: str          # The assistant message containing the tool call
    tool_name: str           # Which tool was called
    tool_args: dict          # Arguments passed to the tool
    success: bool            # Did the tool call succeed?
    error_message: str = ""  # Error message if failed


def extract_messages(db_path: str) -> list[dict[str, Any]]:
    """Extract all messages from the SQLite memory database."""
    if not os.path.exists(db_path):
        print(f"Error: database not found at {db_path}")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.execute("SELECT role, content FROM messages ORDER BY id ASC")
    messages = [{"role": row["role"], "content": row["content"]} for row in cursor]
    conn.close()

    print(f"Extracted {len(messages)} messages from {db_path}")
    return messages


def parse_content(content: str) -> dict | str:
    """Parse a content field which may be a JSON blob or plain string."""
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict) and "content" in parsed:
            return parsed
        return {"content": content}
    except (json.JSONDecodeError, TypeError):
        return {"content": content}


def extract_tool_call_examples(messages: list[dict]) -> list[ToolCallExample]:
    """Walk through messages and extract tool-call sequences.

    Pattern:
      assistant: "Let me read the file. <function=read_file>{"path": "x"}</function>"
      tool: "Success: ..." or "Error: ..."
    """
    examples: list[ToolCallExample] = []
    context_window: list[dict] = []  # Last N messages before tool call

    for i, msg in enumerate(messages):
        role = msg["role"]
        content = parse_content(msg["content"])
        text = content.get("content", str(content))

        if role == "assistant" and "<function=" in text:
            # Build prompt from preceding context
            prompt_text = _build_prompt(context_window[-10:])

            # Parse tool calls from the assistant message
            import re
            tool_matches = re.findall(TOOL_CALL_PATTERN, text, re.DOTALL)

            for tool_name, tool_args_str in tool_matches:
                # Look ahead for the tool result
                success = True
                error_message = ""
                if i + 1 < len(messages):
                    next_msg = messages[i + 1]
                    if next_msg["role"] == "tool":
                        result = parse_content(next_msg["content"])
                        result_text = result.get("content", str(result))
                        if "error" in result_text.lower() or "failed" in result_text.lower():
                            success = False
                            error_message = result_text[:200]

                # Try to parse args
                try:
                    tool_args = json.loads(tool_args_str) if tool_args_str.strip() else {}
                except json.JSONDecodeError:
                    tool_args = {}

                examples.append(ToolCallExample(
                    prompt=prompt_text or "You are mini_agent, a coding assistant.",
                    completion=text,
                    tool_name=tool_name,
                    tool_args=tool_args,
                    success=success,
                    error_message=error_message,
                ))

        context_window.append({"role": role, "text": text[:500]})
        if len(context_window) > 20:
            context_window = context_window[-20:]

    return examples


def _build_prompt(context: list[dict]) -> str:
    """Build a prompt string from recent context messages."""
    if not context:
        return ""

    lines = ["You are mini_agent, a coding agent."]
    for msg in context:
        role_label = "User" if msg["role"] == "user" else msg["role"].capitalize()
        lines.append(f"{role_label}: {msg['text'][:200]}")
    return "\n".join(lines)


def export_jsonl(examples: list[ToolCallExample], output_path: str) -> None:
    """Export training examples to JSONL format for grpo_train.py."""
    with open(output_path, "w") as f:
        for ex in examples:
            record = {
                "prompt": ex.prompt,
                "completion": ex.completion,
                "tool_name": ex.tool_name,
                "success": ex.success,
            }
            f.write(json.dumps(record) + "\n")

    print(f"Exported {len(examples)} training examples to {output_path}")


def print_stats(examples: list[ToolCallExample]) -> None:
    """Print summary statistics about extracted examples."""
    if not examples:
        print("No tool-call examples found.")
        return

    tool_counts: dict[str, int] = {}
    success_count = 0
    failure_count = 0

    for ex in examples:
        tool_counts[ex.tool_name] = tool_counts.get(ex.tool_name, 0) + 1
        if ex.success:
            success_count += 1
        else:
            failure_count += 1

    print(f"\n{'='*60}")
    print("Training Data Summary")
    print(f"{'='*60}")
    print(f"Total examples:           {len(examples)}")
    print(f"Successful tool calls:    {success_count} ({success_count/len(examples)*100:.1f}%)")
    print(f"Failed tool calls:        {failure_count} ({failure_count/len(examples)*100:.1f}%)")
    print("\nTool distribution:")
    for tool, count in sorted(tool_counts.items(), key=lambda x: -x[1]):
        bar = "█" * min(count, 40)
        print(f"  {tool:25s} {count:4d}  {bar}")


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Extract GRPO training data from mini_agent memory"
    )
    parser.add_argument(
        "--db",
        type=str,
        default=".mini_agent_memory.db",
        help="Path to SQLite memory database (default: .mini_agent_memory.db)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="training_data.jsonl",
        help="Output JSONL file path (default: training_data.jsonl)",
    )
    parser.add_argument(
        "--stats-only",
        action="store_true",
        help="Only print statistics, don't export",
    )
    args = parser.parse_args()

    messages = extract_messages(args.db)
    examples = extract_tool_call_examples(messages)

    print_stats(examples)

    if not examples:
        print("\nNo tool-call examples found. Try running mini_agent first to generate conversation history.")
        return

    if not args.stats_only:
        export_jsonl(examples, args.output)


if __name__ == "__main__":
    main()
