"""
搜索节点实现
负责生成搜索查询和反思查询
"""

import json
import os
import sys
from typing import Dict, Any
from json.decoder import JSONDecodeError
from loguru import logger

from .base_node import BaseNode
from ..prompts import SYSTEM_PROMPT_FIRST_SEARCH, SYSTEM_PROMPT_REFLECTION
from ..utils.text_processing import (
    remove_reasoning_from_output,
    clean_json_tags,
    extract_clean_response,
    fix_incomplete_json
)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
from utils.topic_anchor import fallback_search_query

_SEARCH_OUTPUT_KEYS = (
    "search_query",
    "reasoning",
    "search_tool",
    "start_date",
    "end_date",
    "platform",
    "time_period",
    "enable_sentiment",
    "texts",
)


def _extract_search_payload(result: Dict[str, Any]) -> Dict[str, Any]:
    payload = {
        key: result[key]
        for key in _SEARCH_OUTPUT_KEYS
        if key in result and result[key] is not None
    }
    payload.setdefault("search_query", "")
    payload.setdefault("reasoning", "")
    return payload


class FirstSearchNode(BaseNode):
    """为段落生成首次搜索查询的节点"""

    def __init__(self, llm_client):
        super().__init__(llm_client, "FirstSearchNode")
        self._user_query: str = ""

    def validate_input(self, input_data: Any) -> bool:
        if isinstance(input_data, str):
            try:
                data = json.loads(input_data)
                return (
                    "user_query" in data
                    and "title" in data
                    and "content" in data
                )
            except JSONDecodeError:
                return False
        elif isinstance(input_data, dict):
            return (
                "user_query" in input_data
                and "title" in input_data
                and "content" in input_data
            )
        return False

    def run(self, input_data: Any, **kwargs) -> Dict[str, str]:
        try:
            if not self.validate_input(input_data):
                raise ValueError(
                    "输入数据格式错误，需要包含 user_query、title 和 content 字段"
                )

            if isinstance(input_data, str):
                data = json.loads(input_data)
                message = input_data
            else:
                data = input_data
                message = json.dumps(input_data, ensure_ascii=False)

            self._user_query = str(data.get("user_query", ""))
            logger.info("正在生成首次搜索查询")
            response = self.llm_client.stream_invoke_to_string(
                SYSTEM_PROMPT_FIRST_SEARCH, message
            )
            processed_response = self.process_output(response)
            logger.info(f"生成搜索查询: {processed_response.get('search_query', 'N/A')}")
            return processed_response
        except Exception as e:
            logger.exception(f"生成首次搜索查询失败: {str(e)}")
            raise e

    def process_output(self, output: str) -> Dict[str, str]:
        try:
            cleaned_output = remove_reasoning_from_output(output)
            cleaned_output = clean_json_tags(cleaned_output)
            logger.info(f"清理后的输出: {cleaned_output}")
            try:
                result = json.loads(cleaned_output)
                logger.info("JSON解析成功")
            except JSONDecodeError as e:
                logger.error(f"JSON解析失败: {str(e)}")
                result = extract_clean_response(cleaned_output)
                if "error" in result:
                    fixed_json = fix_incomplete_json(cleaned_output)
                    if fixed_json:
                        try:
                            result = json.loads(fixed_json)
                        except JSONDecodeError:
                            return self._get_default_search_query()
                    else:
                        return self._get_default_search_query()

            if not result.get("search_query", ""):
                return self._get_default_search_query()
            return _extract_search_payload(result)
        except Exception as e:
            self.log_error(f"处理输出失败: {str(e)}")
            return self._get_default_search_query()

    def _get_default_search_query(self) -> Dict[str, str]:
        return {
            "search_query": fallback_search_query(self._user_query),
            "reasoning": "由于解析失败，使用基于用户主题的回退搜索查询",
        }


class ReflectionNode(BaseNode):
    """反思段落并生成新搜索查询的节点"""

    def __init__(self, llm_client):
        super().__init__(llm_client, "ReflectionNode")
        self._user_query: str = ""

    def validate_input(self, input_data: Any) -> bool:
        required_fields = [
            "user_query",
            "title",
            "content",
            "paragraph_latest_state",
        ]
        if isinstance(input_data, str):
            try:
                data = json.loads(input_data)
                return all(field in data for field in required_fields)
            except JSONDecodeError:
                return False
        elif isinstance(input_data, dict):
            return all(field in input_data for field in required_fields)
        return False

    def run(self, input_data: Any, **kwargs) -> Dict[str, str]:
        try:
            if not self.validate_input(input_data):
                raise ValueError(
                    "输入数据格式错误，需要包含 user_query、title、content 和 paragraph_latest_state 字段"
                )

            if isinstance(input_data, str):
                message = input_data
                data = json.loads(input_data)
            else:
                data = input_data
                message = json.dumps(input_data, ensure_ascii=False)

            self._user_query = str(data.get("user_query", ""))
            logger.info("正在进行反思并生成新搜索查询")
            response = self.llm_client.stream_invoke_to_string(
                SYSTEM_PROMPT_REFLECTION, message
            )
            processed_response = self.process_output(response)
            logger.info(
                f"反思生成搜索查询: {processed_response.get('search_query', 'N/A')}"
            )
            return processed_response
        except Exception as e:
            logger.exception(f"反思生成搜索查询失败: {str(e)}")
            raise e

    def process_output(self, output: str) -> Dict[str, str]:
        try:
            cleaned_output = remove_reasoning_from_output(output)
            cleaned_output = clean_json_tags(cleaned_output)
            logger.info(f"清理后的输出: {cleaned_output}")
            try:
                result = json.loads(cleaned_output)
            except JSONDecodeError:
                result = extract_clean_response(cleaned_output)
                if "error" in result:
                    fixed_json = fix_incomplete_json(cleaned_output)
                    if fixed_json:
                        try:
                            result = json.loads(fixed_json)
                        except JSONDecodeError:
                            return self._get_default_reflection_query()
                    else:
                        return self._get_default_reflection_query()

            if not result.get("search_query", ""):
                return self._get_default_reflection_query()
            return _extract_search_payload(result)
        except Exception as e:
            logger.exception(f"处理输出失败: {str(e)}")
            return self._get_default_reflection_query()

    def _get_default_reflection_query(self) -> Dict[str, str]:
        return {
            "search_query": fallback_search_query(self._user_query),
            "reasoning": "由于解析失败，使用基于用户主题的回退反思搜索查询",
        }
