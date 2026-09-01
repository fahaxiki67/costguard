"""字段映射模板：保存人工确认结果并提供只读候选推荐。"""

from jiadun.core.mapping.templates import (
    MappingTemplate,
    MappingTemplateRecommendation,
    recommend_mapping_templates,
    save_mapping_template,
)

__all__ = [
    "MappingTemplate",
    "MappingTemplateRecommendation",
    "recommend_mapping_templates",
    "save_mapping_template",
]
