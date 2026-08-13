#!/usr/bin/env python3
"""由 pages-setup 生成: 把仓库内容复制为 mkdocs 站点源 (site_src/)."""
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 仓库根
SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "site_src")  # .pages/site_src (与mkdocs.yml同级)

SOURCES = [
    "README.md",
    "ARCHITECTURE.md",
    "INTERVIEW_PREP.md",
    "SkillForge-项目方案书-v3.md",
    "docs",
    "assets"
]

def main():
    if os.path.exists(SRC):
        shutil.rmtree(SRC)
    os.makedirs(SRC)
    missing = []
    for item in SOURCES:
        p = os.path.join(ROOT, item)
        if not os.path.exists(p):
            missing.append(item)
            continue
        dst = os.path.join(SRC, item)
        if os.path.isdir(p):
            shutil.copytree(p, dst)
        else:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(p, dst)
    if missing:
        print(f"WARN 缺失(已跳过): {missing}")
    # 统计
    n = sum(len(fs) for _, _, fs in os.walk(SRC))
    print(f"site_src 就绪: {n} 个文件")

if __name__ == "__main__":
    main()
