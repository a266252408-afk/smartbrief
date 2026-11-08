"""共享思考档位：仅给已确认支持的模型发送参数。

来源：https://huggingface.co/zai-org/GLM-5.3-Flash#note
GLM-5.3 系列支持 low/high/max；省略时可能采用 max。
"""
import os
import re

from services.usage_service import UsagePersistenceError


def reasoning_options(model: str) -> dict:
    """各 Agent 与独立评测共用环境配置；不按角色提升档位。"""
    effort = os.getenv('MODEL_REASONING_EFFORT', 'high').strip().lower()
    if effort not in {'low', 'high', 'max'}:
        raise UsagePersistenceError('MODEL_REASONING_EFFORT 仅支持 low、high、max')
    if re.fullmatch(r'(?:[^/]+/)?glm-5\.3(?:-[\w.-]+)?', model.lower()):
        return {'reasoning_effort': effort}
    return {}
