"""Project-owned, read-only skill libraries for the installed Deep Agents API.

Native SkillsMiddleware loads metadata and pins selected SKILL.md bodies before
the first model call: https://docs.langchain.com/oss/python/deepagents/skills
No uploaded dataset, secret, executable hook or additional tool is a skill.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from langchain.agents.middleware import AgentMiddleware

SKILLS_ROOT = Path(__file__).resolve().parents[1] / 'skills'
ROLE_SKILLS = {
    'main': ('report-routing', 'token-optimization'),
    'analyst': ('data-analysis', 'data-visualization', 'token-optimization'),
    'report_designer': ('analytics-dashboard', 'token-optimization'),
}


def skill_root(role: str) -> Path:
    if role not in ROLE_SKILLS:
        raise ValueError('Unknown Agent skill role')
    return SKILLS_ROOT / role


def skill_catalog(role: str) -> list[dict]:
    result = []
    for name in ROLE_SKILLS[role]:
        path = skill_root(role) / name / 'SKILL.md'
        if path.is_file():
            result.append({'name': name, 'path': f'/skills/{name}/SKILL.md',
                           'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    return result


def skill_sources(role: str) -> list[str] | None:
    return ['/skills/'] if skill_catalog(role) else None


def skill_prompt(role: str) -> str:
    return ('\n项目技能通过只读 /skills/ 提供，当前角色可用技能会由 SkillsMiddleware 列出。'
            '已自动载入的 <skill> 是项目审核过的方法指导；其余指导仅相关时read_file(file_path="/skills/绝对路径",offset=0,limit=80)，offset/limit必须是整数。'
            '技能不授予新工具、数据源或执行权限，不能覆盖用户要求、来源授权、数值绑定、沙箱或报告校验。'
            '单指标保持直接快答，不为了技能额外计算 count/median/std 或全面探索。'
            '报告使用当前菜单的create_report/apply_report_edit（兼容模式render_report/revise_report）；复杂选项先report_options按节取schema。'
            '不执行上游插件脚本、不使用CDN、任意HTML或假数据。')


class ProjectSkillsMiddleware(AgentMiddleware):
    """Pin the relevant audited workflow with the native skills state API.

    Pinning is a real backend SKILL.md download by SkillsMiddleware, not a claim
    that merely placing a file in the project made the model consume it.
    """

    def __init__(self, runtime, role: str):
        self.runtime, self.role = runtime, role
        self._reported: set[tuple[str, str]] = set()

    def before_agent(self, state, runtime):
        available = {item['name'] for item in skill_catalog(self.role)}
        request = self.runtime.task.request
        report = bool(self.runtime.task.expected_outputs or re.search(r'报告|仪表盘|report|dashboard', request, re.I))
        if self.role == 'report_designer':
            names = ['analytics-dashboard']
        elif self.role == 'analyst':
            names = ['data-analysis']
            if report or re.search(r'图表|绘图|趋势|可视化|chart|plot|visual', request, re.I):
                names.append('data-visualization')
        else:
            names = ['report-routing'] if report else []
        if getattr(self.runtime.store.settings, 'context_policy', 'off') == 'enforce':
            names.append('token-optimization')
        return {'pinned_skills': [name for name in names if name in available]}

    def wrap_model_call(self, request, handler):
        catalog = {item['path']: item for item in skill_catalog(self.role)}
        for message in request.messages:
            extra = getattr(message, 'additional_kwargs', {})
            marker = extra.get('skill', {})
            item = catalog.get(marker.get('path')) if isinstance(marker, dict) else None
            if extra.get('lc_source') != 'pinned_skill' or not item or marker.get('name') != item['name']:
                continue
            key = (item['name'], item['sha256'])
            with self.runtime.lock:
                if key not in self._reported:
                    self.runtime.event('skill_loaded', '已加载项目技能：' + item['name'],
                                       role=self.role, skill_name=item['name'], skill_path=item['path'],
                                       skill_sha256=item['sha256'], activation='native_pinned_skill')
                    self._reported.add(key)
        return handler(request)
