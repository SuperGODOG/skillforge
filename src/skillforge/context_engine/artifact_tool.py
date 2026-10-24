"""InspectArtifactTool: Reads and queries offloaded disk artifacts for specific objects without re-calling external APIs."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from hello_agents.tools import Tool, ToolParameter, ToolResponse


class InspectArtifactTool(Tool):
    """Tool to read and inspect items from an offloaded disk artifact."""

    def __init__(self, allowed_base_dir: Optional[Path] = None) -> None:
        super().__init__(
            name="inspect_artifact",
            description=(
                "Read and inspect detailed entity records from an offloaded disk artifact "
                "(e.g. large package manifests or tracking histories). Allows querying "
                "a specific object by object_id (e.g. package_id) or retrieving a paginated slice, "
                "avoiding redundant external API queries."
            ),
        )
        self.allowed_base_dir = allowed_base_dir.resolve() if allowed_base_dir else None

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="ref_id",
                type="string",
                description="The artifact URI or file path, e.g. 'fs:///path/to/artifacts/call_1_packages.json'",
                required=True,
            ),
            ToolParameter(
                name="object_id",
                type="string",
                description="Optional object identifier to filter by (e.g. package_id 'PKG_007' or order_id).",
                required=False,
            ),
            ToolParameter(
                name="offset",
                type="integer",
                description="Starting index for pagination (default: 0).",
                required=False,
            ),
            ToolParameter(
                name="limit",
                type="integer",
                description="Maximum number of items to return (default: 5).",
                required=False,
            ),
        ]

    def _clean_path(self, raw_ref: str) -> Path:
        s = str(raw_ref).strip()
        if s.startswith("fs://"):
            s = s[5:]
        return Path(s).resolve()

    def run(self, parameters: dict[str, Any]) -> ToolResponse:
        ref_id = parameters.get("ref_id", "")
        if not ref_id:
            return ToolResponse.error("INVALID_PARAM", "Missing required parameter 'ref_id'.")

        target_path = self._clean_path(ref_id)

        # Security check: ensure path is within allowed_base_dir if configured
        if self.allowed_base_dir:
            try:
                target_path.relative_to(self.allowed_base_dir)
            except ValueError:
                return ToolResponse.error(
                    "SECURITY_VIOLATION",
                    f"Security violation: access to '{target_path}' is outside allowed directory.",
                )

        if not target_path.exists() or not target_path.is_file():
            return ToolResponse.error("FILE_NOT_FOUND", f"Artifact file not found: '{target_path}'")

        try:
            raw_content = target_path.read_text(encoding="utf-8")
        except Exception as exc:
            return ToolResponse.error("READ_ERROR", f"Failed to read artifact '{target_path}': {exc}")

        # Parse JSON
        try:
            data = json.loads(raw_content)
        except Exception:
            # Plain text fallback
            return ToolResponse.success(
                text=f"Raw text artifact content ({len(raw_content)} chars):\n{raw_content[:2000]}",
                data={"raw": raw_content},
            )

        object_id = parameters.get("object_id")
        offset = int(parameters.get("offset") or 0)
        limit = int(parameters.get("limit") or 5)

        # Extract items collection
        items: list[Any] = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            for v in data.values():
                if isinstance(v, list) and len(v) > 0:
                    items = v
                    break
            if not items:
                items = [data]

        # 1. Filter by object_id if requested
        if object_id:
            target_id_str = str(object_id).strip()
            found_match = None
            for item in items:
                if isinstance(item, dict):
                    oid = (
                        item.get("package_id")
                        or item.get("id")
                        or item.get("order_id")
                        or item.get("item_id")
                    )
                    if str(oid) == target_id_str:
                        found_match = item
                        break
                    # Also check nested dictionary keys
                    if any(str(val) == target_id_str for val in item.values() if isinstance(val, (str, int))):
                        found_match = item
                        break
                elif str(item) == target_id_str:
                    found_match = item
                    break

            if found_match is not None:
                return ToolResponse.success(
                    text=f"Found object '{target_id_str}' in artifact '{target_path.name}':\n"
                    + json.dumps(found_match, ensure_ascii=False, indent=2),
                    data={"artifact_ref": str(target_path), "object": found_match},
                )
            else:
                return ToolResponse.error(
                    "OBJECT_NOT_FOUND",
                    f"Object '{target_id_str}' not found in artifact '{target_path.name}'. "
                    f"Total items in artifact: {len(items)}.",
                )

        # 2. Paginated list retrieval
        sliced = items[offset : offset + limit]
        summary_text = (
            f"Artifact '{target_path.name}' (total {len(items)} items, "
            f"showing {len(sliced)} items from offset {offset}):\n"
            + json.dumps(sliced, ensure_ascii=False, indent=2)
        )
        return ToolResponse.success(
            text=summary_text,
            data={
                "artifact_ref": str(target_path),
                "total_items": len(items),
                "offset": offset,
                "limit": limit,
                "items": sliced,
            },
        )
