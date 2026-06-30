"""
LLM 服务适配层 - 支持 MiniMax / Qwen 多种后端
通过 config.yaml 的 llm.provider 切换，默认为 minimax
"""
import requests
import logging
import json
from app.config import get_llm_config, get_minimax_config, get_qwen_config, load_config

logger = logging.getLogger(__name__)


# ==================== 适配层接口 ====================

def get_llm_backend(provider=None):
    """Factory: 根据配置返回对应的 LLM 后端，provider 可覆盖配置
    优先读 cfg['backend_type'] 决定 Backend 类（方案A: type 字段显式声明）
    """
    if provider is None:
        cfg = get_llm_config()
    else:
        # provider 指定时，直接用 provider 名查 config
        all_cfg = load_config()
        provider_node = all_cfg.get(provider, {})
        backend_type = provider_node.get('type', 'minimax')
        base = {
            'provider': provider,
            'backend_type': backend_type,
            'max_tokens': all_cfg.get('llm', {}).get('max_tokens', 2048),
        }
        cfg = {**base, **provider_node}
    backend_type = cfg.get('backend_type', 'minimax')
    if backend_type == 'qwen':
        return QwenBackend(cfg)
    return MiniMaxBackend(cfg)


def _build_system_prompt(context_text):
    """统一的 system prompt 格式，供所有后端使用"""
    return f"""你是一个专业的技术助手。请根据提供的知识库内容回答用户的问题。

回答规则：
1. 如果知识库中有相关的内容，请基于内容进行回答
2. 如果知识库中没有相关信息，请明确告知用户"根据现有知识库无法回答该问题"
3. 回答要准确、简洁、有条理

知识库内容：
{context_text}"""


def _build_user_message(query):
    return f"问题：{query}\n\n请根据上面的知识库内容回答问题。"


def generate_answer(context_chunks, query, model=None, provider=None):
    """
    使用当前配置的 LLM 生成回答（非流式）。
    Returns (answer_text, raw_response)
    """
    backend = get_llm_backend(provider=provider)
    return backend.generate(context_chunks, query, model=model)


def generate_answer_stream(context_chunks, query, model=None, provider=None):
    """
    流式生成回答，yield dicts: {'type': 'text', 'content': '...'} 或 {'type': 'error', 'content': '...'}
    """
    backend = get_llm_backend(provider=provider)
    yield from backend.stream(context_chunks, query, model=model)


# ==================== MiniMax 后端 ====================

class MiniMaxBackend:
    """
    MiniMax Anthropic 兼容接口后端
    端点: POST /v1/messages
    认证: Bearer API Key
    """

    def __init__(self, cfg):
        self.api_key = cfg['api_key']
        self.base_url = cfg['base_url']   # https://api.minimaxi.com/anthropic
        self.default_model = cfg['model']  # MiniMax-M2.7
        self.max_tokens = cfg.get('max_tokens', 2048)

    def _post(self, payload, stream=False, timeout=120):
        base = self.base_url.rstrip('/')
        if base.endswith('/v1'):
            url = f'{base}/messages'
        else:
            url = f'{base}/v1/messages'
        resp = requests.post(
            url,
            headers={
                'Authorization': f'Bearer {self.api_key}',
                'Content-Type': 'application/json',
                'anthropic-version': '2023-06-01',
                'anthropic-dangerous-direct-browser-access': 'true',
            },
            json=payload,
            timeout=timeout,
            stream=stream,
        )
        return resp

    def generate(self, context_chunks, query, model=None):
        model = model or self.default_model
        context_text = "\n\n".join(context_chunks)
        system = _build_system_prompt(context_text)
        user_msg = _build_user_message(query)

        logger.info(f"[MiniMax] generate | model={model} | query='{query[:50]}' | chunks={len(context_chunks)}")

        try:
            resp = self._post({
                'model': model,
                'max_tokens': self.max_tokens,
                'system': system,
                'messages': [{'role': 'user', 'content': user_msg}]
            })
            if resp.status_code != 200:
                logger.error(f"[MiniMax] API error {resp.status_code}: {resp.text[:300]}")
                return f'LLM 调用失败：HTTP {resp.status_code}', None

            result = resp.json()
            answer = self._extract_text(result.get('content', []))
            if not answer:
                answer = 'MiniMax 返回内容为空'
            logger.info(f"[MiniMax] Answer generated, length={len(answer)}")
            return answer, result
        except Exception as e:
            logger.error(f"[MiniMax] Request failed: {e}")
            return f'LLM 调用异常：{str(e)}', None

    def stream(self, context_chunks, query, model=None):
        model = model or self.default_model
        context_text = "\n\n".join(context_chunks)
        system = _build_system_prompt(context_text)
        user_msg = _build_user_message(query)

        logger.info(f"[MiniMax] stream | model={model} | query='{query[:50]}' | chunks={len(context_chunks)}")

        try:
            resp = self._post({
                'model': model,
                'max_tokens': self.max_tokens,
                'system': system,
                'messages': [{'role': 'user', 'content': user_msg}],
                'stream': True,
            }, stream=True)

            if resp.status_code != 200:
                logger.error(f"[MiniMax] Stream error {resp.status_code}: {resp.text[:300]}")
                yield {'type': 'error', 'content': f'LLM 调用失败：HTTP {resp.status_code}'}
                return

            for line in resp.iter_lines():
                if line:
                    line = line.decode('utf-8', errors='replace')
                    if line.startswith('data: '):
                        data_str = line[6:].strip()
                        if data_str == '[DONE]':
                            break
                        try:
                            data = json.loads(data_str)
                            if data.get('type') == 'content_block_delta':
                                delta = data.get('delta', {})
                                if delta.get('type') == 'text_delta':
                                    text = delta.get('text', '')
                                    if text:
                                        yield {'type': 'text', 'content': text}
                            elif data.get('type') == 'error':
                                msg = data.get('error', {}).get('message', 'Unknown error')
                                logger.error(f"[MiniMax] Stream error: {msg}")
                                yield {'type': 'error', 'content': msg}
                        except json.JSONDecodeError:
                            continue
        except Exception as e:
            logger.error(f"[MiniMax] Stream failed: {e}")
            yield {'type': 'error', 'content': f'LLM 调用异常：{str(e)}'}

    @staticmethod
    def _extract_text(content_list):
        """Extract text from MiniMax content list which may include thinking/text blocks"""
        for item in content_list:
            if isinstance(item, dict):
                if item.get('type') == 'text':
                    return item.get('text', '')
        return ''


# ==================== Qwen 后端 ====================

class QwenBackend:
    """
    Qwen OpenAI 兼容接口后端
    端点: POST /v1/chat/completions
    认证: 无需 Key
    """

    def __init__(self, cfg):
        self.base_url = cfg['base_url']    # http://10.40.65.220:8080/qwen3_5
        self.default_model = cfg['model']  # Qwen3.5-27B-W8A8
        self.max_tokens = cfg.get('max_tokens', 2048)

    def _post(self, payload, stream=False, timeout=300):
        base = self.base_url.rstrip('/')
        if base.endswith('/v1'):
            url = f'{base}/chat/completions'
        else:
            url = f'{base}/v1/chat/completions'
        resp = requests.post(
            url,
            headers={'Content-Type': 'application/json'},
            json=payload,
            timeout=timeout,
            stream=stream,
        )
        return resp

    def generate(self, context_chunks, query, model=None):
        model = model or self.default_model
        context_text = "\n\n".join(context_chunks)
        system = _build_system_prompt(context_text)
        user_msg = _build_user_message(query)

        logger.info(f"[Qwen] generate | model={model} | query='{query[:50]}' | chunks={len(context_chunks)}")

        try:
            resp = self._post({
                'model': model,
                'max_tokens': self.max_tokens,
                'messages': [
                    {'role': 'system', 'content': system},
                    {'role': 'user', 'content': user_msg}
                ],
                'extra_body': {
                    'chat_template_kwargs': {
                        'enable_thinking': False
                    }
                }
            })
            if resp.status_code != 200:
                logger.error(f"[Qwen] API error {resp.status_code}: {resp.text[:300]}")
                return f'LLM 调用失败：HTTP {resp.status_code}', None

            result = resp.json()
            choices = result.get('choices', [])
            if not choices:
                return 'Qwen 返回内容为空', result
            answer = choices[0].get('message', {}).get('content', '')
            if not answer:
                answer = 'Qwen 返回内容为空'
            logger.info(f"[Qwen] Answer generated, length={len(answer)}")
            return answer, result
        except Exception as e:
            logger.error(f"[Qwen] Request failed: {e}")
            return f'LLM 调用异常：{str(e)}', None

    def stream(self, context_chunks, query, model=None):
        model = model or self.default_model
        context_text = "\n\n".join(context_chunks)
        system = _build_system_prompt(context_text)
        user_msg = _build_user_message(query)

        logger.info(f"[Qwen] stream | model={model} | query='{query[:50]}' | chunks={len(context_chunks)}")

        try:
            resp = self._post({
                'model': model,
                'max_tokens': self.max_tokens,
                'messages': [
                    {'role': 'system', 'content': system},
                    {'role': 'user', 'content': user_msg}
                ],
                'stream': True,
                'extra_body': {
                    'chat_template_kwargs': {
                        'enable_thinking': False
                    }
                },
            }, stream=True)

            if resp.status_code != 200:
                logger.error(f"[Qwen] Stream error {resp.status_code}: {resp.text[:300]}")
                yield {'type': 'error', 'content': f'LLM 调用失败：HTTP {resp.status_code}'}
                return

            for line in resp.iter_lines():
                if line:
                    line = line.decode('utf-8', errors='replace')
                    if line.startswith('data: '):
                        data_str = line[6:].strip()
                        if data_str == '[DONE]':
                            break
                        try:
                            data = json.loads(data_str)
                            choices = data.get('choices', [])
                            if choices:
                                delta = choices[0].get('delta', {})
                                if 'content' in delta and delta['content']:
                                    yield {'type': 'text', 'content': delta['content']}
                        except json.JSONDecodeError:
                            continue
        except Exception as e:
            logger.error(f"[Qwen] Stream failed: {e}")
            yield {'type': 'error', 'content': f'LLM 调用异常：{str(e)}'}
