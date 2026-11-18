"""清洗执行引擎：清洗链路唯一的判定与编排实现（共用逻辑收拢为一份）。

设计约束（与重构前严格对齐）：

1. ``cleanse_row`` 是逐行清洗判定的唯一入口，原
   ``CleanseRuleService.apply_cleanse`` 的入参到出参结果逐字保持一致，
   不改变空值填充、正则不命中替换的任何分支与默认值口径。
2. 规则按「执行顺序」稳定排序后依次作用于同一批数据行，报告里的规则顺序
   即真实执行顺序；历史策略仍按原策略（当前生效条目）读取，版本选择口径
   不变。
3. 跨月边界按业务日期（数据行 ``dt``，缺失时回退执行日期）判定并归档到
   对应业务月份。
4. 清洗策略中断后只续写未完成部分：以行为粒度记录断点，续写时跳过已处理
   行，仅处理剩余行，命中数在原报告基础上累加。
5. 每次执行额外产出清洗规则执行报告（规则编号、原始值、正则命中位置、
   填充值、替换原因、命中数等），报告与清洗共用同一条代码路径。
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from app.store import store

CLEANSING_MODULE = "cleanse_rule"

# 业务日期候选字段，按优先级取第一个非空值；都缺失时回退执行当天。
BUSINESS_DATE_FIELDS = ("dt", "业务日期", "biz_date", "data_date")
# 中断后允许续写的运行状态。
RESUMABLE_STATUSES = ("中断",)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _business_date_of(row: dict[str, Any], fallback: str) -> str:
    """返回数据行的业务日期（ISO yyyy-mm-dd），缺失时回退到执行日期。"""
    for field in BUSINESS_DATE_FIELDS:
        raw = row.get(field)
        if raw is None or str(raw).strip() == "":
            continue
        text = str(raw).strip()
        # 兼容 "2026-11-01T10:00:00" 这类时间戳，只取日期部分。
        head = text.split("T", 1)[0].split(" ", 1)[0]
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", head):
            return head
    return fallback


def business_month_of(business_date: str) -> str:
    """业务日期所属月份（yyyy-mm），跨月边界判定统一口径。"""
    return business_date[:7]


def sort_rules(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按「执行顺序」升序稳定排序；无法解析的顺序排到最后，保持原相对次序。"""

    def key(indexed: tuple[int, dict[str, Any]]) -> tuple[int, float, int]:
        index, rule = indexed
        raw = str(rule.get("执行顺序", "") or "").strip()
        try:
            return (0, float(raw), index)
        except ValueError:
            return (1, 0.0, index)

    ordered = [rule for _, rule in sorted(enumerate(rules), key=key)]
    return ordered


def compile_pattern(regex_str: str) -> "re.Pattern[str] | None":
    """编译正则；空表达式返回 ``None``（与历史 ``re.compile`` 守卫口径一致）。"""
    return re.compile(regex_str) if regex_str else None


def _null_fill_value(null_handle: str, replace_val: str) -> str:
    """空值填充值：与历史实现同口径。"""
    if "填充为" in null_handle:
        return null_handle.replace("填充为", "")
    return replace_val or "0"


# ---------------------------------------------------------------------------
# 逐行清洗（唯一判定实现）
# ---------------------------------------------------------------------------

def cleanse_row(
    row: dict[str, Any],
    rule: dict[str, Any],
    pattern: "re.Pattern[str] | None",
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """对单行应用一条清洗规则。

    返回 ``(清洗后的新行, 命中明细)``；未发生替换时命中明细为 ``None``。
    判定分支与历史 ``apply_cleanse`` 完全一致：

    - 值为 ``None`` 或 ``""`` 且空值处理含「填充」-> 空值填充；
    - 否则若配置了正则且 ``pattern.search`` 不命中 -> 替换为替换值；
    - 其他情况原样透传。
    """
    new_row = dict(row)
    target_field = rule.get("目标字段", "")
    replace_val = rule.get("替换值", "")
    null_handle = rule.get("空值处理", "")
    original = new_row.get(target_field)

    if original is None or original == "":
        if "填充" in null_handle:
            filled = _null_fill_value(null_handle, replace_val)
            new_row[target_field] = filled
            detail = {
                "hit_type": "空值填充",
                "original_value": "" if original is None else original,
                "filled_value": filled,
                "regex_hit_span": None,
                "reason": f"字段 {target_field} 为空，按空值处理「{null_handle}」填充",
                "changed": True,
            }
            return new_row, detail
        return new_row, None

    if pattern and not pattern.search(str(original)):
        new_row[target_field] = replace_val
        detail = {
            "hit_type": "正则替换",
            "original_value": original,
            "filled_value": replace_val,
            "regex_hit_span": None,
            "reason": f"字段 {target_field} 的值「{original}」未命中正则，替换为「{replace_val}」",
            "changed": True,
        }
        return new_row, detail

    # 合规透传：记录正则命中位置，供报告逐条复核。
    hit_span = None
    if pattern is not None:
        matched = pattern.search(str(original))
        if matched:
            hit_span = list(matched.span())
    return new_row, {
        "hit_type": "正则命中",
        "original_value": original,
        "filled_value": original,
        "regex_hit_span": hit_span,
        "reason": f"字段 {target_field} 的值「{original}」命中正则，原样保留",
        "changed": False,
    }


def _regex_hit_position(pattern: "re.Pattern[str] | None", value: Any) -> list[int] | None:
    """返回正则命中位置 ``[起, 止]``；未配置正则或未命中返回 ``None``。"""
    if pattern is None:
        return None
    matched = pattern.search(str(value))
    if not matched:
        return None
    start, end = matched.span()
    return [start, end]


# ---------------------------------------------------------------------------
# 规则快照与冲突差异
# ---------------------------------------------------------------------------

def _rule_snapshot(rule: dict[str, Any]) -> dict[str, Any]:
    """固化参与执行的规则口径，报告按此快照复核，避免后续被改动。"""
    return {
        "rule_id": rule.get("id"),
        "rule_no": rule.get("策略编号", ""),
        "target_field": rule.get("目标字段", ""),
        "null_handle": rule.get("空值处理", ""),
        "abnormal_threshold": rule.get("异常阈值", ""),
        "regex": rule.get("正则表达式", ""),
        "replace_value": rule.get("替换值", ""),
        "version": rule.get("策略版本", ""),
        "exec_order": rule.get("执行顺序", ""),
    }


def _diff_rule_versions(
    baseline: dict[str, Any], current: dict[str, Any]
) -> list[dict[str, Any]]:
    """对比同位置历史/当前策略，产出变化字段清单（具体程度排序：正则>替换值>空值处理>其余）。"""
    tracked = ["regex", "replace_value", "null_handle", "abnormal_threshold",
               "target_field", "exec_order", "version"]
    specificity = {
        "regex": 0,
        "replace_value": 1,
        "null_handle": 2,
        "abnormal_threshold": 3,
        "target_field": 4,
        "exec_order": 5,
        "version": 6,
    }
    changes = []
    for field in tracked:
        old_value = baseline.get(field)
        new_value = current.get(field)
        if old_value != new_value:
            changes.append({
                "field": field,
                "historical_value": old_value,
                "current_value": new_value,
                "specificity_rank": specificity[field],
            })
    changes.sort(key=lambda item: item["specificity_rank"])
    return changes


# ---------------------------------------------------------------------------
# 报告记录
# ---------------------------------------------------------------------------

def _new_rule_record(rule: dict[str, Any]) -> dict[str, Any]:
    return {
        "rule_no": rule.get("策略编号", ""),
        "rule_id": rule.get("id"),
        "target_field": rule.get("目标字段", ""),
        "rule_version": rule.get("策略版本", ""),
        "exec_order": rule.get("执行顺序", ""),
        "rule_snapshot": _rule_snapshot(rule),
        "total_rows": 0,
        "hit_count": 0,          # 命中数：实际发生空值填充/正则替换的行数
        "null_fill_count": 0,
        "regex_replace_count": 0,
        "regex_hit_count": 0,    # 正则校验命中（合规）行数，用于口径复核
        "changed_fields": [rule.get("目标字段", "")],
        "hit_details": [],       # 逐条：规则编号/原始值/正则命中位置/填充值/替换原因
        "months": {},            # 业务月份 -> 命中数，支撑跨月边界收拢
    }


def _record_hit(
    record: dict[str, Any],
    rule: dict[str, Any],
    pattern: "re.Pattern[str] | None",
    row: dict[str, Any],
    detail: dict[str, Any],
    business_date: str,
) -> None:
    month = business_month_of(business_date)
    record["total_rows"] += 1
    record["hit_count"] += 1
    record["months"][month] = record["months"].get(month, 0) + 1
    if detail["hit_type"] == "空值填充":
        record["null_fill_count"] += 1
    else:
        record["regex_replace_count"] += 1
    record["hit_details"].append({
        "rule_no": rule.get("策略编号", ""),
        "rule_version": rule.get("策略版本", ""),
        "target_field": rule.get("目标字段", ""),
        "business_date": business_date,
        "business_month": month,
        "original_value": detail["original_value"],
        "regex_hit_span": detail.get("regex_hit_span"),
        "filled_value": detail["filled_value"],
        "hit_type": detail["hit_type"],
        "reason": detail["reason"],
    })


def _record_pass(
    record: dict[str, Any],
    pattern: "re.Pattern[str] | None",
    row: dict[str, Any],
    target_field: str,
    business_date: str,
    detail: dict[str, Any] | None = None,
) -> None:
    record["total_rows"] += 1
    if pattern is None:
        return
    hit_span = detail.get("regex_hit_span") if detail else _regex_hit_position(
        pattern, row.get(target_field))
    if hit_span is not None:
        month = business_month_of(business_date)
        record["regex_hit_count"] += 1
        # 跨月边界收拢：合规命中也按业务月份归集，保证各月计数完整可复核。
        record["months"].setdefault(month, 0)
        record["hit_details"].append({
            "rule_no": record["rule_no"],
            "rule_version": record["rule_version"],
            "target_field": target_field,
            "business_date": business_date,
            "business_month": month,
            "original_value": row.get(target_field),
            "regex_hit_span": hit_span,
            "filled_value": row.get(target_field),
            "hit_type": "正则命中",
            "reason": f"字段 {target_field} 的值命中正则，原样保留",
        })


# ---------------------------------------------------------------------------
# 数据集级清洗编排
# ---------------------------------------------------------------------------

def _conflicts_for(rules: list[dict[str, Any]], dataset: str) -> list[dict[str, Any]]:
    """同执行顺序位置上历史版本与当前版本冲突时的差异清单。

    历史报告保留原版本顺序与命中数；这里仅把同位置冲突的变化字段记录到
    差异清单，具体程度排序，不改变执行优先级（当前生效条目优先的既有口径）。
    """
    conflicts: list[dict[str, Any]] = []
    order_groups: dict[str, list[dict[str, Any]]] = {}
    for rule in rules:
        order_groups.setdefault(str(rule.get("执行顺序", "")), []).append(rule)
    for exec_order, group in order_groups.items():
        if len(group) < 2:
            continue
        baseline = _rule_snapshot(group[0])
        for current_rule in group[1:]:
            current = _rule_snapshot(current_rule)
            changes = _diff_rule_versions(baseline, current)
            if changes:
                conflicts.append({
                    "exec_order": exec_order,
                    "historical_rule_no": baseline["rule_no"],
                    "historical_version": baseline["version"],
                    "current_rule_no": current["rule_no"],
                    "current_version": current["version"],
                    "priority": "待确认",
                    "changed_fields": changes,
                })
    return conflicts


def run_dataset_cleanse(
    dataset: str,
    data_rows: list[dict[str, Any]],
    *,
    rules: list[dict[str, Any]] | None = None,
    run_id: str | None = None,
    business_date: str | None = None,
    stop_after: int | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """对一个数据集按执行顺序依次应用清洗规则，并生成清洗规则执行报告。

    参数
    ----
    dataset: 目标数据集（表名）。
    data_rows: 待清洗原始数据行。
    rules: 显式指定规则；默认按数据集从清洗策略模块读取（历史策略原口径）。
    run_id: 续写已有运行时传入；不传则新建一次运行。
    business_date: 执行业务日期（跨月边界用），默认当天。
    stop_after: 处理多少行后模拟中断（用于断点续写），``None`` 表示不中断。
    persist: 是否把报告写入 store。

    返回运行报告 dict，其中 ``rows`` 为清洗后数据行（与逐规则 apply 结果一致）。
    """
    run_date = business_date or date.today().isoformat()
    if rules is None:
        rules = store.find_all_by_field(CLEANSING_MODULE, "目标数据集", dataset)
    ordered_rules = sort_rules(rules)

    now = datetime.now().isoformat()
    if run_id is not None:
        existing = store.get_cleanse_run(run_id)
        if existing is None:
            raise ValueError(f"清洗执行运行 {run_id} 不存在，无法续写")
        run = existing
        # 续写只处理未完成部分，已处理行保持原结果。
        processed = run.get("processed_rows", 0)
        if processed > len(data_rows):
            raise ValueError("续写输入行数少于已处理行数，输入与历史运行不一致")
        # 同步本次传入的数据：保留已处理行的清洗结果，仅刷新尚未处理的行，
        # 保证独立调用续写时入参与运行内数据一致。
        run["rows"] = run.get("rows", [])[:processed] + [dict(row) for row in data_rows[processed:]]
        run["total_input_rows"] = len(data_rows)
        run["resumed_at"] = now
    else:
        run_id = run_id or store.new_cleanse_run_id(run_date)
        run = {
            "run_id": run_id,
            "dataset": dataset,
            "business_date": run_date,
            "business_month": business_month_of(run_date),
            "status": "运行中",
            "started_at": now,
            "finished_at": None,
            "total_input_rows": len(data_rows),
            "processed_rows": 0,
            "rows": [dict(row) for row in data_rows],
            "records": [],
            "conflicts": _conflicts_for(ordered_rules, dataset),
            "quality_writebacks": [],
        }

    # 规则记录按执行顺序建立；续写时复用既有记录，命中数在原报告上累加。
    records = run["records"]
    record_by_no = {rec["rule_no"]: rec for rec in records}
    compiled: list[tuple[dict[str, Any], "re.Pattern[str] | None", dict[str, Any]]] = []
    for rule in ordered_rules:
        pattern = compile_pattern(rule.get("正则表达式", ""))
        record = record_by_no.get(rule.get("策略编号", ""))
        if record is None:
            record = _new_rule_record(rule)
            records.append(record)
        compiled.append((rule, pattern, record))

    working_rows = run["rows"]
    start_index = run["processed_rows"]
    interrupted = False
    end_index = len(data_rows)
    if stop_after is not None:
        end_index = min(end_index, start_index + max(stop_after, 0))
        if end_index < len(data_rows):
            interrupted = True

    for index in range(start_index, end_index):
        row = working_rows[index]
        row_biz_date = _business_date_of(row, run_date)
        for rule, pattern, record in compiled:
            target_field = rule.get("目标字段", "")
            current_value = row.get(target_field)
            # 统计口径：每行每规则都计入 total_rows；命中与正则合规分别计数。
            if current_value is None or current_value == "":
                if "填充" in rule.get("空值处理", ""):
                    new_row, detail = cleanse_row(row, rule, pattern)
                    row.clear()
                    row.update(new_row)
                    _record_hit(record, rule, pattern, row, detail, row_biz_date)
                else:
                    record["total_rows"] += 1
                continue
            if pattern is not None:
                new_row, detail = cleanse_row(row, rule, pattern)
                if detail is not None and detail["changed"]:
                    row.clear()
                    row.update(new_row)
                    _record_hit(record, rule, pattern, row, detail, row_biz_date)
                else:
                    _record_pass(record, pattern, row, target_field, row_biz_date, detail)
            else:
                # 无正则且非空：历史实现原样透传，不产生命中，但仍计入评估总行数。
                record["total_rows"] += 1

    run["processed_rows"] = end_index
    if interrupted:
        run["status"] = "中断"
        run["finished_at"] = None
    else:
        run["status"] = "已完成"
        run["finished_at"] = datetime.now().isoformat()

    if persist:
        store.save_cleanse_run(run)
    return run


def resume_cleanse_run(
    run_id: str,
    data_rows: list[dict[str, Any]], *, stop_after: int | None = None
) -> dict[str, Any]:
    """中断后续写：只处理未完成部分，沿用同一份规则快照与报告。"""
    existing = store.get_cleanse_run(run_id)
    if existing is None:
        raise ValueError(f"清洗执行运行 {run_id} 不存在")
    if existing.get("status") not in RESUMABLE_STATUSES:
        raise ValueError(f"运行 {run_id} 状态为 {existing.get('status')}，无需续写")
    return run_dataset_cleanse(
        existing["dataset"],
        data_rows,
        run_id=run_id,
        business_date=existing.get("business_date"),
        stop_after=stop_after,
    )
