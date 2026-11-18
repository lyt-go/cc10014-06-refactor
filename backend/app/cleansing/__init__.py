"""清洗执行链路统一收拢包。

把原先散落在 ``CleanseRuleService.apply_cleanse`` 与
``DwdLayerService.build_dwd_from_ods`` 里的清洗判定、规则编排、版本选择、
跨月边界、断点续写与清洗规则执行报告逻辑集中到一处，保证：

- 入参到出参的逐行处理结果与历史版本完全一致（同一套判定函数，无第二份实现）；
- 清洗规则执行报告与清洗执行共用同一条代码路径，报告只是执行过程的副产物；
- 历史策略仍按原策略读取（当前生效条目），版本选择口径不变。
"""
from __future__ import annotations

from app.cleansing.engine import (
    business_month_of,
    cleanse_row,
    compile_pattern,
    run_dataset_cleanse,
    resume_cleanse_run,
    sort_rules,
)

__all__ = [
    "business_month_of",
    "cleanse_row",
    "compile_pattern",
    "run_dataset_cleanse",
    "resume_cleanse_run",
    "sort_rules",
]
