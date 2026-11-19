#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
达梦数据库连接工具类
提供统一的dmPython连接接口，处理模式切换等细节
"""

import sys
from pathlib import Path
from typing import Optional, List, Tuple, Any
from loguru import logger

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.append(str(project_root))

try:
    import dmPython
except ImportError:
    logger.error("dmPython未安装，请安装: pip install dmPython")
    dmPython = None

from config import settings


class DMConnection:
    """达梦数据库连接类"""
    
    def __init__(self, config: Optional[dict] = None):
        """
        初始化达梦数据库连接
        
        Args:
            config: 数据库配置字典，包含 host, port, user, password, database
                    如果为None，则从settings读取
        """
        if config is None:
            self.config = {
                'host': settings.DB_HOST or "127.0.0.1",
                'port': settings.DB_PORT or 5236,
                'user': settings.DB_USER or "SYSDBA",
                'password': settings.DB_PASSWORD or "",
                'database': settings.DB_NAME or "mindspider"
            }
        else:
            self.config = config
        
        self.connection: Optional[Any] = None
        self.cursor: Optional[Any] = None
    
    def connect(self) -> bool:
        """
        连接到达梦数据库并切换到目标模式
        
        Returns:
            连接是否成功
        """
        if dmPython is None:
            logger.error("dmPython未安装")
            return False
        
        try:
            # 连接到达梦数据库
            self.connection = dmPython.connect(
                user=self.config['user'],
                password=self.config['password'],
                server=self.config['host'],
                port=self.config['port']
            )
            
            self.cursor = self.connection.cursor()
            
            # 设置UTF-8字符集
            try:
                self.cursor.execute("ALTER SESSION SET NLS_CHARACTERSET='UTF8'")
                self.cursor.execute("ALTER SESSION SET NLS_NCHAR_CHARACTERSET='UTF8'")
                self.connection.commit()
            except Exception as e:
                logger.warning(f"设置字符集警告: {e}")
            
            # 切换到目标模式（重要！）
            db_name = self.config['database']
            schema_names = [
                f'"{db_name}"',              # 原始大小写加引号
                f'"{db_name.upper()}"',      # 大写加引号
                f'"{db_name.lower()}"',     # 小写加引号
            ]
            
            schema_switched = False
            for schema_name in schema_names:
                try:
                    self.cursor.execute(f'ALTER SESSION SET CURRENT_SCHEMA={schema_name}')
                    self.connection.commit()
                    logger.debug(f"成功切换到模式: {schema_name}")
                    schema_switched = True
                    break
                except Exception as schema_error:
                    logger.debug(f"尝试模式名 {schema_name} 失败: {schema_error}")
                    continue
            
            if not schema_switched:
                logger.warning(f"无法切换到模式 {db_name}，将使用默认模式")
            
            logger.info(f"达梦数据库连接成功: {self.config['user']}@{self.config['host']}:{self.config['port']}")
            return True
            
        except Exception as e:
            logger.error(f"达梦数据库连接失败: {e}")
            import traceback
            logger.debug(traceback.format_exc())
            return False
    
    def execute(self, sql: str, params: Optional[tuple] = None) -> Optional[List[Tuple]]:
        """
        执行SQL语句
        
        Args:
            sql: SQL语句
            params: 参数元组（可选）
        
        Returns:
            查询结果列表（如果是SELECT）或None
        """
        if not self.cursor:
            raise RuntimeError("数据库未连接，请先调用connect()")
        
        try:
            if params:
                self.cursor.execute(sql, params)
            else:
                self.cursor.execute(sql)
            
            # 如果是SELECT语句，返回结果
            if sql.strip().upper().startswith('SELECT'):
                return self.cursor.fetchall()
            
            self.connection.commit()
            return None
            
        except Exception as e:
            self.connection.rollback()
            logger.error(f"SQL执行失败: {e}")
            logger.debug(f"SQL: {sql}")
            raise
    
    def fetchall(self) -> List[Tuple]:
        """获取所有查询结果"""
        if not self.cursor:
            raise RuntimeError("数据库未连接")
        return self.cursor.fetchall()
    
    def fetchone(self) -> Optional[Tuple]:
        """获取一条查询结果"""
        if not self.cursor:
            raise RuntimeError("数据库未连接")
        return self.cursor.fetchone()
    
    def close(self):
        """关闭数据库连接"""
        if self.cursor:
            self.cursor.close()
            self.cursor = None
        if self.connection:
            self.connection.close()
            self.connection = None
    
    def __enter__(self):
        """上下文管理器入口"""
        if not self.connect():
            raise RuntimeError("无法连接到数据库")
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器出口"""
        self.close()