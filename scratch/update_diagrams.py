import os

diagrams = [
    'docs/skillforge-architecture.html',
    'docs/skillforge-task-sequence.html',
    'docs/skillforge-evolution-workflow.html',
    'docs/skillforge-skill-lifecycle.html'
]

link_to_add = '<a href="skillforge-knowledge-index.html#resume-qa-matrix" style="padding: 3px 8px; border-radius: 4px; background: rgba(245, 158, 11, 0.15); color: #fbbf24; text-decoration: none; border: 1px solid rgba(245, 158, 11, 0.35); font-size: 11px; font-weight: 600; transition: all 0.2s;">📋 简历与21题库</a> '

for path in diagrams:
    if not os.path.exists(path):
        continue
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    if 'resume-qa-matrix' in content:
        print(f'{path} already contains resume-qa-matrix link.')
        continue
    
    # Insert right after `<span style="opacity: 0.8; font-weight: 600; color: #fbbf24;">💡 核心知识索引：</span>`
    target = '<span style="opacity: 0.8; font-weight: 600; color: #fbbf24;">💡 核心知识索引：</span>'
    if target in content:
        content = content.replace(target, target + '\n        ' + link_to_add)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f'Successfully updated {path}!')
    else:
        print(f'Target not found in {path}')

