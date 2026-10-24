"""Object-Aware Large Output Offloading for SkillForge Context Engine.

Replaces brute-force head/tail truncation with:
1. Complete raw payload saved to disk artifacts.
2. Structured in-context summary preserving object count, ID list, partial preview,
   and explicit coverage warnings to prevent hallucinated completeness.
3. Plain-text logs indexed with line counts and head/tail previews.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class OffloadResult:
    """Outcome of an offloading operation."""

    is_offloaded: bool = False
    artifact_file: Optional[Path] = None
    in_context_replacement: str = ""
    total_count: int = 0
    expanded_count: int = 0
    unexpanded_count: int = 0
    object_ids: list[str] = field(default_factory=list)
    total_lines: int = 0


class LargeOutputOffloader:
    """Detects and offloads large observations to disk artifacts."""

    def __init__(
        self,
        threshold_chars: int = 1500,
        max_preview_items: int = 2,
    ) -> None:
        self.threshold_chars = threshold_chars
        self.max_preview_items = max_preview_items

    def should_offload(self, raw_text: str, data: Any = None) -> bool:
        """Determine whether output exceeds the in-context budget."""
        if len(raw_text or "") > self.threshold_chars:
            return True
        if isinstance(data, list) and len(data) >= 3:
            return True
        if isinstance(data, dict):
            for v in data.values():
                if isinstance(v, list) and len(v) >= 3:
                    return True
        return False

    def offload(
        self,
        run_id: str,
        tool_name: str,
        call_id: str,
        raw_text: str,
        data: Any,
        artifacts_dir: Path,
    ) -> OffloadResult:
        """Offload raw observation to disk artifact and return structured in-context replacement."""
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        # Detect structured object collections
        items_list, id_key = self._extract_items_list(data)

        if items_list is not None and len(items_list) > 0:
            # 1. Structured object offloading path
            artifact_file = artifacts_dir / f"{call_id}_{tool_name}.json"
            artifact_file.write_text(
                json.dumps(data if data is not None else {"raw": raw_text}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            total_count = len(items_list)
            expanded_count = min(self.max_preview_items, total_count)
            unexpanded_count = total_count - expanded_count

            # Extract IDs
            object_ids = []
            for item in items_list:
                if isinstance(item, dict):
                    oid = item.get(id_key or "id") or item.get("package_id") or item.get("order_id")
                    if oid:
                        object_ids.append(str(oid))
                else:
                    object_ids.append(str(item))

            preview_items = []
            for item in items_list[:expanded_count]:
                if isinstance(item, dict):
                    compact_item = {}
                    for k, v in item.items():
                        if isinstance(v, str) and len(v) > 60:
                            compact_item[k] = v[:57] + "..."
                        elif isinstance(v, list) and len(v) > 2:
                            compact_item[k] = v[:2] + [f"... ({len(v) - 2} more items)"]
                        else:
                            compact_item[k] = v
                    preview_items.append(compact_item)
                else:
                    preview_items.append(item)

            replacement_dict = {
                "_type": "OBJECT_AWARE_OFFLOADED_SUMMARY",
                "tool_name": tool_name,
                "total_count": total_count,
                "expanded_count": expanded_count,
                "unexpanded_count": unexpanded_count,
                "object_ids": object_ids,
                "preview_items": preview_items,
                "artifact_ref": f"fs://{artifact_file.resolve()}",
                "coverage_warning": (
                    f"Warning: Total {total_count} items exist in full record. "
                    f"Only {expanded_count} expanded above; {unexpanded_count} remaining items unexpanded. "
                    "Do not assume unexpanded items are non-existent or all delivered. "
                    "Query individual items specifically if needed."
                ),
            }

            return OffloadResult(
                is_offloaded=True,
                artifact_file=artifact_file,
                in_context_replacement=json.dumps(replacement_dict, ensure_ascii=False),
                total_count=total_count,
                expanded_count=expanded_count,
                unexpanded_count=unexpanded_count,
                object_ids=object_ids,
                total_lines=0,
            )
        else:
            # 2. Plain text / log offloading path
            artifact_file = artifacts_dir / f"{call_id}_{tool_name}.log"
            artifact_file.write_text(raw_text, encoding="utf-8")

            lines = raw_text.splitlines()
            total_lines = len(lines)
            head_lines = lines[:3]
            tail_lines = lines[-3:] if total_lines > 3 else []

            head_text = "\n".join(head_lines)
            tail_text = "\n".join(tail_lines)

            replacement = (
                f"[OFFLOADED_TEXT_ARTIFACT]\n"
                f"Tool: {tool_name} (Total {total_lines} lines, {len(raw_text)} chars)\n"
                f"Artifact Ref: fs://{artifact_file.resolve()}\n"
                f"Line range: 1-{total_lines}\n"
                f"Head preview:\n{head_text[:120]}\n"
                f"...\n"
                f"Tail preview:\n{tail_text[:120]}\n"
                "Instruction: Full output offloaded to disk. Inspect specific sections via inspect_artifact."
            )

            return OffloadResult(
                is_offloaded=True,
                artifact_file=artifact_file,
                in_context_replacement=replacement,
                total_count=0,
                expanded_count=0,
                unexpanded_count=0,
                object_ids=[],
                total_lines=total_lines,
            )

    def _extract_items_list(self, data: Any) -> Tuple[Optional[list], Optional[str]]:
        """Identify if data contains a collection of objects."""
        if isinstance(data, list):
            return data, "id"
        if isinstance(data, dict):
            for k in ("packages", "orders", "items", "records", "results", "events"):
                if isinstance(data.get(k), list):
                    return data[k], k[:-1] + "_id"
            for k, v in data.items():
                if isinstance(v, list) and len(v) > 0 and isinstance(v[0], dict):
                    return v, "id"
        return None, None
