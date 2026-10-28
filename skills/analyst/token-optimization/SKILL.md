---
name: token-optimization
description: Keep the current task scoped, reuse verified results and reduce repeated context, tools and handoffs without losing evidence or permissions.
---
# Token Optimization 项目适配
固定上游 bm629/agent-skills 6f1504cfc0e39fed36f6d7405907231d6474c01d，MIT。完整原件在项目vendor，只按需读取references。
每题仅计算用户所需指标；已有相同口径结果优先复用。APP_STATE是压缩账本，保留原需求、来源、base/hash/targets与全部报告版本。按当前阶段选择工具，批量独立读取，typed need_analysis不再复述。相同参数成功不重跑；连续无进展错误改策略。缺事实不填数字，不绕过权限或降低用户问题范围。只在确有长上下文时原生offload；先查来源细节再扩展上下文。
