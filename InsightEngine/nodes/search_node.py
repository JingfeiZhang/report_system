"""
搜索节点实现
负责生成搜索查询和反思查询
"""

import json
import os
import sys
from typing import Dict, Any, Optional
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
        """
        初始化首次搜索节点
        
        Args:
            llm_client: LLM客户端
        """
        super().__init__(llm_client, "FirstSearchNode")
        self._user_query: str = ""
    
    def validate_input(self, input_data: Any) -> bool:
        """验证输入数据"""
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
        """
        调用LLM生成搜索查询和理由
        
        Args:
            input_data: 包含title和content的字符串或字典
            **kwargs: 额外参数
            
        Returns:
            包含search_query和reasoning的字典
        """
        try:
            if not self.validate_input(input_data):
                raise ValueError(
                    "输入数据格式错误，需要包含 user_query、title 和 content 字段"
                )
            
            # 准备输入数据
            if isinstance(input_data, str):
                data = json.loads(input_data)
                message = input_data
            else:
                data = input_data
                message = json.dumps(input_data, ensure_ascii=False)

            self._user_query = str(data.get("user_query", ""))
            
            logger.info("正在生成首次搜索查询")
            
            # 调用LLM（流式，安全拼接UTF-8）
            response = self.llm_client.stream_invoke_to_string(SYSTEM_PROMPT_FIRST_SEARCH, message)
            
            # 处理响应
            processed_response = self.process_output(response)
            
            logger.info(f"生成搜索查询: {processed_response.get('search_query', 'N/A')}")
            return processed_response
            
        except Exception as e:
            logger.exception(f"生成首次搜索查询失败: {str(e)}")
            raise e
    
    def process_output(self, output: str) -> Dict[str, str]:
        """
        处理LLM输出，提取搜索查询和推理
        
        Args:
            output: LLM原始输出
            
        Returns:
            包含search_query和reasoning的字典
        """
        try:
            # 清理响应文本
            cleaned_output = remove_reasoning_from_output(output)
            cleaned_output = clean_json_tags(cleaned_output)
            
            # 记录清理后的输出用于调试
            logger.info(f"清理后的输出: {cleaned_output}")
            
            # 解析JSON
            try:
                result = json.loads(cleaned_output)
                logger.info("JSON解析成功")
            except JSONDecodeError as e:
                logger.error(f"JSON解析失败: {str(e)}")
                # 使用更强大的提取方法
                result = extract_clean_response(cleaned_output)
                if "error" in result:
                    logger.error("JSON解析失败，尝试修复...")
                    # 尝试修复JSON
                    fixed_json = fix_incomplete_json(cleaned_output)
                    if fixed_json:
                        try:
                            result = json.loads(fixed_json)
                            logger.info("JSON修复成功")
                        except JSONDecodeError:
                            logger.error("JSON修复失败")
                            # 返回默认查询
                            return self._get_default_search_query()
                    else:
                        logger.error("无法修复JSON，使用默认查询")
                        return self._get_default_search_query()
            
            # 验证和清理结果
            search_query = result.get("search_query", "")
            if not search_query:
                logger.warning("未找到搜索查询，使用默认查询")
                return self._get_default_search_query()

            return _extract_search_payload(result)
            
        except Exception as e:
            self.log_error(f"处理输出失败: {str(e)}")
            # 返回默认查询
            return self._get_default_search_query()
    
    def _get_default_search_query(self) -> Dict[str, str]:
        """
        获取默认搜索查询
        
        Returns:
            默认的搜索查询字典
        """
        return {
            "search_query": fallback_search_query(self._user_query),
            "reasoning": "由于解析失败，使用基于用户主题的回退搜索查询",
        }


class ReflectionNode(BaseNode):
    """反思段落并生成新搜索查询的节点"""
    
    def __init__(self, llm_client):
        """
        初始化反思节点
        
        Args:
            llm_client: LLM客户端
        """
        super().__init__(llm_client, "ReflectionNode")
        self._user_query: str = ""
    
    def validate_input(self, input_data: Any) -> bool:
        """验证输入数据"""
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
        """
        调用LLM反思并生成搜索查询
        
        Args:
            input_data: 包含title、content和paragraph_latest_state的字符串或字典
            **kwargs: 额外参数
            
        Returns:
            包含search_query和reasoning的字典
        """
        try:
            if not self.validate_input(input_data):
                raise ValueError(
                    "输入数据格式错误，需要包含 user_query、title、content 和 paragraph_latest_state 字段"
                )
            
            # 准备输入数据
            if isinstance(input_data, str):
                message = input_data
                data = json.loads(input_data)
            else:
                data = input_data
                message = json.dumps(input_data, ensure_ascii=False)

            self._user_query = str(data.get("user_query", ""))

            logger.info("正在进行反思并生成新搜索查询")
            response = self.llm_client.stream_invoke_to_string(SYSTEM_PROMPT_REFLECTION, message)
            
            # 处理响应
            processed_response = self.process_output(response)
            
            logger.info(f"反思生成搜索查询: {processed_response.get('search_query', 'N/A')}")
            return processed_response
            
        except Exception as e:
            logger.exception(f"反思生成搜索查询失败: {str(e)}")
            raise e
    
    def process_output(self, output: str) -> Dict[str, str]:
        """
        处理LLM输出，提取搜索查询和推理
        
        Args:
            output: LLM原始输出
            
        Returns:
            包含search_query和reasoning的字典
        """
        try:
            # 清理响应文本
            cleaned_output = remove_reasoning_from_output(output)
            cleaned_output = clean_json_tags(cleaned_output)
            
            # 记录清理后的输出用于调试
            logger.info(f"清理后的输出: {cleaned_output}")
            
            # 解析JSON
            try:
                result = json.loads(cleaned_output)
                logger.info("JSON解析成功")
            except JSONDecodeError as e:
                logger.error(f"JSON解析失败: {str(e)}")
                # 使用更强大的提取方法
                result = extract_clean_response(cleaned_output)
                if "error" in result:
                    logger.error("JSON解析失败，尝试修复...")
                    # 尝试修复JSON
                    fixed_json = fix_incomplete_json(cleaned_output)
                    if fixed_json:
                        try:
                            result = json.loads(fixed_json)
                            logger.info("JSON修复成功")
                        except JSONDecodeError:
                            logger.error("JSON修复失败")
                            # 返回默认查询
                            return self._get_default_reflection_query()
                    else:
                        logger.error("无法修复JSON，使用默认查询")
                        return self._get_default_reflection_query()
            
            # 验证和清理结果
            search_query = result.get("search_query", "")
            if not search_query:
                logger.warning("未找到搜索查询，使用默认查询")
                return self._get_default_reflection_query()

            return _extract_search_payload(result)
            
        except Exception as e:
            logger.exception(f"处理输出失败: {str(e)}")
            # 返回默认查询
            return self._get_default_reflection_query()
    
    def _get_default_reflection_query(self) -> Dict[str, str]:
        """
        获取默认反思搜索查询
        
        Returns:
            默认的反思搜索查询字典
        """
        return {
            "search_query": fallback_search_query(self._user_query),
            "reasoning": "由于解析失败，使用基于用户主题的回退反思搜索查询",
        }
