"""第二层的执行器编译适配器。

UI Procedure 编译器通过兼容名称按需加载，避免默认导入 pandas。
"""

from .database import DatabaseCompiler
from .http import HttpApiCompiler
from .performance import PerformanceCompiler
from .port import TcpPortCompiler

__all__ = [
    "DatabaseCompiler",
    "HttpApiCompiler",
    "PerformanceCompiler",
    "TcpPortCompiler",
]


def __getattr__(name: str):
    """Keep the UI Procedure compiler lazy while preserving its public name."""

    if name == "ProcedureStageCompiler":
        from .procedure import ProcedureStageCompiler

        return ProcedureStageCompiler
    raise AttributeError(name)
