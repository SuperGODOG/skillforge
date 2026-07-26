"""SkillRegistry：元数据索引 + use_skill 特殊工具

继承 hello_agents.tools.ToolRegistry
- load_skills_from_dir(): T4 ✓
- build_index():          T4 ✓
- get_current_release():  T6 ✓
- use_skill():            T6 ✓（Agent 主导渐进式披露的核心入口）

参见方案书 §4.2、ARCHITECTURE §4-A/§7
"""
from __future__ import annotations
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hello_agents.tools import ToolRegistry

from .models import SkillMeta, Release
from .storage.jsonl import append as jsonl_append


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


class SkillRegistry(ToolRegistry):
    def __init__(
        self,
        db_path: Path,
        skills_dir: Path,
        repo_root: Optional[Path] = None,
        router_log: Optional[Path] = None,
    ):
        super().__init__()
        self.db_path = db_path
        self.skills_dir = skills_dir
        self.repo_root = repo_root or skills_dir.parent
        self.router_log = router_log or (self.repo_root / "runs" / "router.jsonl")
        self._metas: dict[str, SkillMeta] = {}
        self._bodies: dict[str, str] = {}
        self._sm = None  # lazy ReleaseStateMachine

    def close(self) -> None:
        if self._sm is not None:
            self._sm.close()
            self._sm = None

    # ---------- T4：加载与索引 ----------

    def load_skills_from_dir(self, path: Optional[Path] = None) -> None:
        """扫 skills/*/SKILL.md → Pydantic 解析为 SkillMeta，填 _metas / _bodies"""
        import yaml  # 局部 import：不影响顶层 import skillforge

        path = path or self.skills_dir
        if not path.exists():
            raise FileNotFoundError(f"skills 目录不存在：{path}")

        for skill_md in sorted(path.glob("*/SKILL.md")):
            text = skill_md.read_text(encoding="utf-8")
            m = _FRONTMATTER_RE.match(text)
            if not m:
                raise ValueError(f"{skill_md}：缺少 YAML frontmatter (--- ... ---)")
            frontmatter_text, body = m.group(1), m.group(2)
            data = yaml.safe_load(frontmatter_text) or {}
            meta = SkillMeta(**data)
            if meta.name in self._metas:
                raise ValueError(f"Skill name 冲突：{meta.name}（{skill_md}）")
            self._metas[meta.name] = meta
            self._bodies[meta.name] = body.strip()

    def build_index(self) -> str:
        if not self._metas:
            return "## Available Skills\n\n（当前无已注册 Skill）"

        lines = ["## Available Skills\n"]
        for name, meta in sorted(self._metas.items()):
            lines.append(f"### {name}")
            lines.append(f"- description: {meta.description}")
            lines.append(f"- use_when: {meta.use_when}")
            if meta.not_for:
                lines.append(f"- not_for: {', '.join(meta.not_for)}")
            lines.append("")
        lines.append("---")
        lines.append(
            "如需完整说明书，调用 use_skill(name, reason)。"
            "reason 必须写明为什么本次任务要用该 Skill（供审计与路由日志归因）。"
        )
        return "\n".join(lines)

    def get_meta(self, name: str) -> SkillMeta:
        if name not in self._metas:
            raise KeyError(f"skill 未注册：{name}")
        return self._metas[name]

    def list_names(self) -> list[str]:
        return sorted(self._metas.keys())

    # ---------- T6：SQLite → Git → Body 全链路 ----------

    def _get_sm(self):
        if self._sm is None:
            from .state_machine import ReleaseStateMachine
            self._sm = ReleaseStateMachine(self.db_path, self.repo_root)
        return self._sm

    def get_current_release(self, name: str) -> Optional[Release]:
        sm = self._get_sm()
        release_id = sm.get_current_release_id(name)
        if not release_id:
            return None
        d = sm.get_release(release_id)
        if not d:
            return None
        return Release(
            release_id=d["release_id"],
            skill_name=d["skill_name"],
            version=d["version"],
            commit_hash=d["commit_hash"],
            status=d["status"],
            level=d["level"],
        )

    def use_skill(self, name: str, reason: str) -> str:
        """加载指定 Skill 的完整说明书 Body（Agent 通过 ReAct 显式调用）

        优先级：SQLite→Git 读发布版本 Body；失败降级到磁盘 body。
        每次调用都写 router.jsonl 日志（含 reason 归因、source 标记、latency）。

        Args:
            name:   Skill 标识
            reason: 加载理由（必填，用于路由日志归因）

        Returns:
            Skill 完整 Body 文本；未注册返回 [ERROR] 前缀的可读错误
            （Agent 可读到并降级，不抛异常打断 ReAct）。
        """
        started = datetime.now(timezone.utc)

        if name not in self._metas:
            available = self.list_names()
            self._log(started, name, reason,
                      status="not_found", source=None, release_id=None,
                      extra={"available": available})
            return f"[ERROR] skill '{name}' 未注册。可用: {available}"

        body: str
        source: str
        release = self.get_current_release(name)
        if release and release.commit_hash:
            try:
                from .storage.git_ops import read_file_at_commit
                rel_path = f"skills/{name}/SKILL.md"
                full = read_file_at_commit(self.repo_root, release.commit_hash, rel_path)
                m = _FRONTMATTER_RE.match(full)
                body = m.group(2).strip() if m else full.strip()
                source = "git"
            except Exception as e:
                body = self._bodies[name]
                source = f"disk_fallback:{type(e).__name__}"
        else:
            body = self._bodies[name]
            source = "disk_no_release"

        latency_ms = (datetime.now(timezone.utc) - started).total_seconds() * 1000
        self._log(started, name, reason,
                  status="ok", source=source,
                  release_id=release.release_id if release else None,
                  extra={"latency_ms": round(latency_ms, 2), "body_chars": len(body)})
        return body

    def _log(
        self,
        started: datetime,
        name: str,
        reason: str,
        status: str,
        source: Optional[str],
        release_id: Optional[str],
        extra: Optional[dict] = None,
    ) -> None:
        record = {
            "ts": started.isoformat(),
            "op": "use_skill",
            "name": name,
            "reason": reason,
            "status": status,
            "source": source,
            "release_id": release_id,
        }
        if extra:
            record.update(extra)
        try:
            jsonl_append(self.router_log, record)
        except Exception:
            pass  # 日志失败绝不影响主流程
