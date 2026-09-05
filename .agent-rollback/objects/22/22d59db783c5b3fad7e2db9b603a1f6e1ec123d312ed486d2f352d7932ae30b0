"""JSONL 追加写：evaluations.jsonl / router.jsonl

单进程模型下的简单追加，非线程安全。
"""
from __future__ import annotations
from pathlib import Path
import json
from typing import Any


def append(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_all(path: Path) -> list[dict]:
    """读全部记录（Phase 4 元 Agent 收集失败样本用）"""
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]
