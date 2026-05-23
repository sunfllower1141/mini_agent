#!/usr/bin/env python3
"""
grpo_train.py — GRPO fine-tuning for mini_agent tool-use optimization.

Uses Unsloth (memory-efficient) + TRL GRPOTrainer to train a model
on tool-selection accuracy using Group Relative Policy Optimization.

Designed for: RTX 6000 Ada 48GB
Base model:    Qwen2.5-7B-Instruct (4-bit, ~16GB VRAM)
Training data: mini_agent conversation history with tool calls

Usage:
    source ~/grpo_env/bin/activate
    python grpo_train.py --data /path/to/training_data.jsonl

Training takes ~30 min for 300 steps (test), 12+ hours for full run.

References:
    - Unsloth GRPO: https://docs.unsloth.ai/get-started/reinforcement-learning-rl-guide
    - TRL GRPOTrainer: https://huggingface.co/docs/trl/grpo_trainer
    - ToolGRPO: https://github.com/okaybroda/ToolGRPO
    - ART (Agent RL Trainer): https://docs.unsloth.ai/get-started/rl/art
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any

import torch
from datasets import Dataset
from trl import GRPOConfig, GRPOTrainer
from unsloth import FastLanguageModel, is_bfloat16_supported

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_MODEL = "unsloth/Qwen2.5-7B-Instruct"
MAX_SEQ_LENGTH = 2048
LORA_RANK = 32
LORA_ALPHA = LORA_RANK * 2
NUM_GENERATIONS = 4  # Size of the group for GRPO advantage calculation
MAX_COMPLETION_LENGTH = 512
MAX_PROMPT_LENGTH = 512
LEARNING_RATE = 5e-6
MAX_STEPS = 300
OUTPUT_DIR = "grpo_tool_agent_output"

# Tool names from mini_agent's TOOLS schema (subset for initial training)
KNOWN_TOOLS = {
    "read_file", "write_file", "edit_file", "list_directory", "file_info",
    "write_scratchpad", "run_shell", "run_tests", "search_files", "find_symbol",
    "find_usages", "web_search", "fetch_url", "remember", "plan", "plan_status",
    "task_status", "diff", "restore_file", "verify", "diagnose_failures",
    "spawn_agent", "agent_status", "collect_agent", "agent_message",
    "git", "use_skill", "session_stats", "recall_turn",
}

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class TrainingExample:
    """A single training example extracted from mini_agent memory."""
    prompt: str            # The conversation context leading up to a tool call
    completion: str        # The expected tool call + reasoning
    tool_name: str         # Which tool was called
    success: bool          # Whether the tool call succeeded


# ---------------------------------------------------------------------------
# Reward functions
# ---------------------------------------------------------------------------


def _extract_tool_calls(text: str) -> list[dict[str, str]]:
    """Extract tool call blocks from model output.

    Returns list of {"name": tool_name, "args": args_json_string}.
    """
    pattern = r'<function=(\w+)>(.*?)</function>'
    matches = re.findall(pattern, text, re.DOTALL)
    return [{"name": name, "args": args.strip()} for name, args in matches]


def _parse_json_args(args_str: str) -> dict[str, Any] | None:
    """Safely parse JSON arguments string."""
    try:
        return json.loads(args_str)
    except (json.JSONDecodeError, TypeError):
        # Try to repair truncated JSON
        cleaned = re.sub(r',\s*}', '}', args_str)
        cleaned = re.sub(r',\s*\]', ']', cleaned)
        try:
            return json.loads(cleaned)
        except (json.JSONDecodeError, TypeError):
            return None


def reward_tool_format(completions: list[str], **kwargs) -> list[float]:
    """Reward for correctly formatted tool calls.

    Rewards:
    - 0.5: At least one valid tool call block found
    - 0.3: Tool name is a known mini_agent tool
    - 0.2: Arguments are valid JSON
    - 0.0: No tool call found
    """
    rewards = []
    for completion in completions:
        score = 0.0
        tool_calls = _extract_tool_calls(completion)
        if tool_calls:
            score += 0.5
            tc = tool_calls[0]
            if tc["name"] in KNOWN_TOOLS:
                score += 0.3
            if _parse_json_args(tc["args"]) is not None:
                score += 0.2
        rewards.append(score)
    return rewards


def reward_tool_selection(completions: list[str], expected_tool: list[str] | None = None, **kwargs) -> list[float]:
    """Reward for selecting the correct tool given the context.

    If expected_tool is provided (from training data), reward correct selection.
    Otherwise, reward reasonable selections based on heuristics.
    """
    rewards = []
    for i, completion in enumerate(completions):
        tool_calls = _extract_tool_calls(completion)
        if not tool_calls:
            rewards.append(0.0)
            continue

        called_tool = tool_calls[0]["name"]

        # If we have ground truth, use exact match
        if expected_tool and i < len(expected_tool) and expected_tool[i]:
            rewards.append(1.0 if called_tool == expected_tool[i] else 0.0)
            continue

        # Heuristic: reward any known tool call
        rewards.append(0.5 if called_tool in KNOWN_TOOLS else 0.0)

    return rewards


def reward_task_completion(completions: list[str], success: list[bool] | None = None, **kwargs) -> list[float]:
    """Reward based on whether the tool call would likely succeed.

    Checks for:
    - Proper argument structure
    - Reasonable parameter values
    - Not repeating previously-failed patterns
    """
    rewards = []
    for i, completion in enumerate(completions):
        score = 0.0
        tool_calls = _extract_tool_calls(completion)

        if not tool_calls:
            rewards.append(0.0)
            continue

        tc = tool_calls[0]
        args = _parse_json_args(tc["args"])

        # Basic sanity: tool has required args
        if args is not None:
            score += 0.3

        # Tool-specific heuristics
        if tc["name"] == "read_file" and args and "path" in args:
            score += 0.2  # Has path parameter
        elif tc["name"] == "edit_file" and args:
            if all(k in args for k in ("path", "old_string", "new_string")):
                score += 0.4  # Has all required params
        elif tc["name"] == "run_shell" and args and "command" in args:
            score += 0.3  # Has command
        elif tc["name"] == "search_files" and args and "pattern" in args:
            score += 0.2

        # Ground truth success signal
        if success and i < len(success) and success[i]:
            score = 1.0
        elif success and i < len(success) and not success[i]:
            score = 0.0

        rewards.append(min(score, 1.0))

    return rewards


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def load_mini_agent_data(data_path: str) -> Dataset:
    """Load training data from mini_agent JSONL format.

    Expected format (one JSON object per line):
    {
        "prompt": "<conversation context as a single string>",
        "completion": "<expected tool call + reasoning>",
        "tool_name": "read_file",
        "success": true
    }
    """
    examples = []
    with open(data_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            examples.append(TrainingExample(
                prompt=obj["prompt"],
                completion=obj["completion"],
                tool_name=obj.get("tool_name", ""),
                success=obj.get("success", True),
            ))

    # Format as GRPO dataset: one column with prompt, rest in kwargs
    dataset_dict = {
        "prompt": [ex.prompt for ex in examples],
        "expected_tool": [ex.tool_name for ex in examples],
        "success": [ex.success for ex in examples],
        "reference_completion": [ex.completion for ex in examples],
    }
    return Dataset.from_dict(dataset_dict)


def create_synthetic_dataset() -> Dataset:
    """Create a small synthetic dataset of tool-use conversations.

    Used for testing the training pipeline before using real data.
    """
    examples = [
        {
            "prompt": (
                "You are mini_agent, a coding agent. You need to read a file.\n"
                "User: What does config.py contain?"
            ),
            "completion": (
                'I need to read the file to answer. '
                '<function=read_file>{"path": "/workspace/config.py"}</function>'
            ),
            "tool_name": "read_file",
            "success": True,
        },
        {
            "prompt": (
                "You are mini_agent, a coding agent. You need to search the codebase.\n"
                "User: Where is the SafetyResult class defined?"
            ),
            "completion": (
                'I should use find_symbol to locate the class definition. '
                '<function=find_symbol>{"name": "SafetyResult"}</function>'
            ),
            "tool_name": "find_symbol",
            "success": True,
        },
        {
            "prompt": (
                "You are mini_agent, a coding agent. You need to edit a file.\n"
                "User: Change the max_tokens default from 100000 to 200000 in config.py"
            ),
            "completion": (
                'First I need to read config.py to see the exact text, then edit. '
                '<function=read_file>{"path": "config.py"}</function>'
            ),
            "tool_name": "read_file",
            "success": True,
        },
        {
            "prompt": (
                "You are mini_agent, a coding agent. User wants to run tests.\n"
                "User: Run the test suite for memory.py"
            ),
            "completion": (
                'I will run the specific test file. '
                '<function=run_tests>{"path": "test_memory.py"}</function>'
            ),
            "tool_name": "run_tests",
            "success": True,
        },
        {
            "prompt": (
                "You are mini_agent, a coding agent. User wants to search the web.\n"
                "User: Find the latest Python asyncio best practices"
            ),
            "completion": (
                'I need to search the web for this. '
                '<function=web_search>{"query": "Python asyncio best practices 2025", "num_results": 5}</function>'
            ),
            "tool_name": "web_search",
            "success": True,
        },
        {
            "prompt": (
                "You are mini_agent, a coding agent. Edit failed because string not found.\n"
                "Tool result: edit_file failed - old_string not found in file.\n"
                "You need to fix this."
            ),
            "completion": (
                'The edit failed because the string wasn\'t found. I should read the file '
                'first to see the exact text, then try again. '
                '<function=read_file>{"path": "config.py", "line_numbers": true}</function>'
            ),
            "tool_name": "read_file",
            "success": True,
        },
        {
            "prompt": (
                "You are mini_agent, a coding agent. Shell command timed out.\n"
                "User: Install all dependencies"
            ),
            "completion": (
                'The previous command timed out. I should try pip install --quiet. '
                '<function=run_shell>{"command": "pip install --quiet -r requirements.txt", "timeout": 300}</function>'
            ),
            "tool_name": "run_shell",
            "success": True,
        },
        {
            "prompt": (
                "You are mini_agent, a coding agent. Multi-step plan needed.\n"
                "User: Refactor the memory module to use async/await"
            ),
            "completion": (
                'This is a complex task. I should create a plan first. '
                '<function=plan>{"steps": ["Read current memory.py", "Identify sync functions to convert", '
                '"Rewrite with asyncio", "Update callers", "Run tests"]}</function>'
            ),
            "tool_name": "plan",
            "success": True,
        },
        {
            "prompt": (
                "You are mini_agent, a coding agent. Spawn sub-agents for parallel work.\n"
                "User: Write tests for config.py, memory.py, and safety.py in parallel"
            ),
            "completion": (
                'I can use fan_out to spawn parallel sub-agents for each test file. '
                'First, I need to activate the agents skill. '
                '<function=use_skill>{"name": "agents"}</function>'
            ),
            "tool_name": "use_skill",
            "success": True,
        },
        {
            "prompt": (
                "You are mini_agent, a coding agent. Session is long.\n"
                "User: What have we accomplished so far?"
            ),
            "completion": (
                'Let me check the session statistics to summarize progress. '
                '<function=session_stats>{}</function>'
            ),
            "tool_name": "session_stats",
            "success": True,
        },
    ]

    dataset_dict = {
        "prompt": [ex["prompt"] for ex in examples],
        "expected_tool": [ex["tool_name"] for ex in examples],
        "success": [ex["success"] for ex in examples],
        "reference_completion": [ex["completion"] for ex in examples],
    }
    return Dataset.from_dict(dataset_dict)


# ---------------------------------------------------------------------------
# Model loading
# ---------------------------------------------------------------------------


def load_model():
    """Load the base model with Unsloth for memory-efficient training."""
    print(f"Loading model: {BASE_MODEL}")
    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=BASE_MODEL,
        max_seq_length=MAX_SEQ_LENGTH,
        dtype=None,  # Auto-detect
        load_in_4bit=True,  # 4-bit quantization — ~16GB for 7B model
        fast_inference=True,  # Enable vLLM fast inference
        max_lora_rank=LORA_RANK,
        gpu_memory_utilization=0.6,  # Leave room for vLLM generations
    )

    # Configure LoRA
    model = FastLanguageModel.get_peft_model(
        model,
        r=LORA_RANK,
        target_modules=[
            "q_proj", "k_proj", "v_proj", "o_proj",
            "gate_proj", "up_proj", "down_proj",
        ],
        lora_alpha=LORA_ALPHA,
        lora_dropout=0.0,
        bias="none",
        use_gradient_checkpointing="unsloth",  # Memory saving
        random_state=3407,
        use_rslora=False,
        loftq_config=None,
    )

    return model, tokenizer


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------


def train(model, tokenizer, dataset: Dataset, output_dir: str = OUTPUT_DIR):
    """Run GRPO training with tool-calling reward functions."""

    training_args = GRPOConfig(
        # vLLM sampling
        vllm_sampling_params=None,  # Use defaults
        temperature=1.0,  # Higher = more diverse generations

        # Learning
        learning_rate=LEARNING_RATE,
        weight_decay=0.01,
        warmup_ratio=0.1,
        lr_scheduler_type="cosine",
        optim="adamw_8bit",

        # Batch
        per_device_train_batch_size=1,
        gradient_accumulation_steps=1,

        # GRPO specific
        num_generations=NUM_GENERATIONS,  # Group size for advantage
        max_prompt_length=MAX_PROMPT_LENGTH,
        max_completion_length=MAX_COMPLETION_LENGTH,
        beta=0.001,  # KL penalty coefficient

        # Duration
        max_steps=MAX_STEPS,
        num_train_epochs=1,

        # Precision
        bf16=is_bfloat16_supported(),
        fp16=not is_bfloat16_supported(),
        gradient_checkpointing=True,
        gradient_checkpointing_kwargs={"use_reentrant": False},

        # Logging
        logging_steps=10,
        save_steps=100,
        save_total_limit=3,
        report_to="none",  # Set to "wandb" for experiment tracking
        output_dir=output_dir,

        # vLLM integration
        use_vllm=True,
        vllm_device="auto",
        vllm_gpu_memory_utilization=0.3,  # Reserve for training
    )

    trainer = GRPOTrainer(
        model=model,
        processing_class=tokenizer,
        reward_funcs=[
            reward_tool_format,
            reward_tool_selection,
            reward_task_completion,
        ],
        args=training_args,
        train_dataset=dataset,
    )

    print(f"Starting GRPO training for {MAX_STEPS} steps...")
    print(f"  Model: {BASE_MODEL}")
    print(f"  LoRA rank: {LORA_RANK}")
    print(f"  Generations per prompt: {NUM_GENERATIONS}")
    print(f"  Max completion length: {MAX_COMPLETION_LENGTH}")
    print(f"  Output dir: {output_dir}")
    print(f"  Dataset size: {len(dataset)} examples")

    trainer.train()

    # Save the LoRA adapter
    model.save_lora(f"{output_dir}/lora_adapter")
    print(f"LoRA adapter saved to {output_dir}/lora_adapter")

    return trainer


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="GRPO fine-tuning for mini_agent tool-use optimization"
    )
    parser.add_argument(
        "--data",
        type=str,
        default=None,
        help="Path to JSONL training data file (uses synthetic data if not provided)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=OUTPUT_DIR,
        help=f"Output directory (default: {OUTPUT_DIR})",
    )
    parser.add_argument(
        "--steps",
        type=int,
        default=MAX_STEPS,
        help=f"Number of training steps (default: {MAX_STEPS})",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=BASE_MODEL,
        help=f"Base model to fine-tune (default: {BASE_MODEL})",
    )
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help="Use synthetic training data (for testing)",
    )
    args = parser.parse_args()

    # Override constants from CLI
    global MAX_STEPS, BASE_MODEL
    MAX_STEPS = args.steps
    BASE_MODEL = args.model

    # Load data
    if args.data and not args.synthetic:
        print(f"Loading training data from {args.data}")
        dataset = load_mini_agent_data(args.data)
    else:
        print("Using synthetic training data")
        dataset = create_synthetic_dataset()

    print(f"Dataset: {len(dataset)} training examples")

    # Load model
    model, tokenizer = load_model()

    # Train
    trainer = train(model, tokenizer, dataset, output_dir=args.output_dir)

    print(f"\nTraining complete! Model saved to {args.output_dir}")
    print("To use the trained model:")
    print(f"  model.save_pretrained_merged('{args.output_dir}/merged')")
    print(f"  # Or for GGUF: model.save_pretrained_gguf('{args.output_dir}/gguf')")


if __name__ == "__main__":
    main()
