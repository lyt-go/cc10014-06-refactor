"""清洗链路重构测试：统一执行引擎 + 清洗规则执行报告。

覆盖：
- 重构前后入参/出参、替换值与历史口径完全一致（apply_cleanse 契约不变）
- 报告逐条记录规则编号、原始值、正则命中位置、填充值、替换原因
- 命中数（空值填充/正则替换/正则合规）口径
- 执行顺序稳定排序、多规则顺序
- 业务日期跨月边界收拢
- 中断后只续写未完成部分，命中数在原报告累加
- 历史/当前版本冲突：保留原顺序与命中数，差异清单按具体程度排序
- 质量稽核跨模块回写报告，稽核通过率不变
"""
from __future__ import annotations

import pytest

from app.cleansing.engine import business_month_of, sort_rules
from app.seed import SEED_ROWS
from app.services.cleanse_rule import CleanseRuleService
from app.services.quality_check import QualityCheckService
from app.store import store


def reset_store():
    store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
    store.reset_runtime()


@pytest.fixture(autouse=True)
def _reset():
    reset_store()
    yield


# ---------------------------------------------------------------------------
# 1. 历史口径不变：apply_cleanse 入参到出参与重构前一致
# ---------------------------------------------------------------------------
class TestLegacyParity:
    def test_apply_cleanse_matches_legacy_outputs(self):
        cs = CleanseRuleService()
        data = [
            {"order_id": "O001", "order_amount": "100.50"},
            {"order_id": "O002", "order_amount": "-10.00"},
            {"order_id": "O003", "order_amount": ""},
        ]
        result = cs.apply_cleanse(1, data)
        # 与历史测试逐字一致
        assert result[0]["order_amount"] == "100.50"
        assert result[1]["order_amount"] == "0.00"
        assert result[2]["order_amount"] == "0.00"

    def test_apply_cleanse_does_not_mutate_input(self):
        cs = CleanseRuleService()
        data = [{"order_amount": ""}]
        cs.apply_cleanse(1, data)
        assert data[0]["order_amount"] == ""

    def test_apply_cleanse_unknown_entry_returns_input(self):
        cs = CleanseRuleService()
        data = [{"x": 1}]
        assert cs.apply_cleanse(9999, data) == data

    def test_fill_to_keyword_uses_suffix(self):
        # 空值处理含「填充为」时取后缀，与历史实现一致
        cs = CleanseRuleService()
        entry, _ = cs.create_entry({
            "策略编号": "CLN-X", "目标字段": "v", "空值处理": "填充为9",
            "正则表达式": "", "替换值": "0", "策略版本": "v1", "执行顺序": "1",
            "目标数据集": "t_x",
        })
        out = cs.apply_cleanse(entry["id"], [{"v": ""}])
        assert out[0]["v"] == "9"

    def test_fill_without_keyword_defaults_replace_or_zero(self):
        cs = CleanseRuleService()
        entry, _ = cs.create_entry({
            "策略编号": "CLN-Y", "目标字段": "v", "空值处理": "填充默认",
            "正则表达式": "", "替换值": "", "策略版本": "v1", "执行顺序": "1",
            "目标数据集": "t_y",
        })
        out = cs.apply_cleanse(entry["id"], [{"v": ""}])
        assert out[0]["v"] == "0"


# ---------------------------------------------------------------------------
# 2. 报告字段逐条核对：规则编号/原始值/正则位置/填充值/替换原因/命中数
# ---------------------------------------------------------------------------
class TestReportFields:
    def _rows(self):
        return [
            {"order_id": "A", "order_amount": "100.50", "dt": "2026-10-31"},
            {"order_id": "B", "order_amount": "bad", "dt": "2026-11-01"},
            {"order_id": "C", "order_amount": "", "dt": "2026-11-02"},
            {"order_id": "D", "order_amount": "200.00", "dt": "2026-12-01"},
        ]

    def test_report_records_rule_no_values_spans_reasons(self):
        cs = CleanseRuleService()
        run = cs.execute_dataset_cleanse("ods_orders", self._rows(), business_date="2026-11-18")
        assert run["status"] == "已完成"
        record = run["records"][0]
        assert record["rule_no"] == "CLN-ORD-001"
        # 命中数口径：2 条实际替换（1 正则替换 + 1 空值填充），2 条正则合规
        assert record["hit_count"] == 2
        assert record["regex_replace_count"] == 1
        assert record["null_fill_count"] == 1
        assert record["regex_hit_count"] == 2
        assert record["total_rows"] == 4

        by_id = {detail["original_value"] if detail["original_value"] != "" else "<empty>": detail
                 for detail in record["hit_details"]}
        # 规则编号、填充值、替换原因逐条可核对
        bad = by_id["bad"]
        assert bad["rule_no"] == "CLN-ORD-001"
        assert bad["filled_value"] == "0.00"
        assert bad["regex_hit_span"] is None
        assert "未命中正则" in bad["reason"]
        assert bad["hit_type"] == "正则替换"

        empty = by_id["<empty>"]
        assert empty["filled_value"] == "0.00"
        assert empty["hit_type"] == "空值填充"
        assert "为空" in empty["reason"]

        good = by_id["100.50"]
        assert good["regex_hit_span"] == [0, 6]
        assert good["hit_type"] == "正则命中"

    def test_cleansed_rows_identical_to_legacy_apply(self):
        cs = CleanseRuleService()
        rows = self._rows()
        run = cs.execute_dataset_cleanse("ods_orders", rows, business_date="2026-11-18")
        legacy = cs.apply_cleanse(1, [dict(r) for r in rows])
        assert [r["order_amount"] for r in run["rows"]] == [r["order_amount"] for r in legacy]

    def test_business_month_filter(self):
        cs = CleanseRuleService()
        cs.execute_dataset_cleanse("ods_orders", self._rows(), business_date="2026-11-18")
        nov = cs.list_reports(business_month="2026-11")
        assert len(nov) == 1
        assert cs.list_reports(business_month="2026-09") == []

    def test_total_rows_covers_every_input_row_even_without_regex(self):
        cs = CleanseRuleService()
        # 无正则规则：所有非空值透传，但 total_rows 仍应等于输入行数
        entry, _ = cs.create_entry({
            "策略编号": "CLN-NOPAT", "目标字段": "v", "空值处理": "不处理",
            "正则表达式": "", "替换值": "", "策略版本": "v1", "执行顺序": "1",
            "目标数据集": "ds_nopat",
        })
        rows = [{"v": "a"}, {"v": "b"}, {"v": ""}, {"v": "c"}]
        run = cs.execute_dataset_cleanse("ds_nopat", rows, business_date="2026-11-18")
        record = run["records"][0]
        assert record["total_rows"] == 4
        assert record["hit_count"] == 0
        # 透传不改变值
        assert [r["v"] for r in run["rows"]] == ["a", "b", "", "c"]


# ---------------------------------------------------------------------------
# 3. 跨月边界按业务日期收拢
# ---------------------------------------------------------------------------
class TestCrossMonthBoundary:
    def test_month_derived_from_row_business_date(self):
        assert business_month_of("2026-10-31") == "2026-10"
        assert business_month_of("2026-11-01T08:00:00".split("T")[0]) == "2026-11"

    def test_hit_details_grouped_by_row_month(self):
        cs = CleanseRuleService()
        rows = [
            {"order_amount": "100.50", "dt": "2026-10-31"},
            {"order_amount": "bad", "dt": "2026-11-01"},
            {"order_amount": "200.00", "dt": "2026-12-01"},
        ]
        run = cs.execute_dataset_cleanse("ods_orders", rows, business_date="2026-11-18")
        details = run["records"][0]["hit_details"]
        months = {d["business_month"] for d in details}
        # 数据行的业务日期跨 10/11/12 三个月，而非统一归到执行月
        assert months == {"2026-10", "2026-11", "2026-12"}
        # 运行本身按执行业务日期归档
        assert run["business_month"] == "2026-11"

    def test_missing_dt_falls_back_to_run_date(self):
        cs = CleanseRuleService()
        rows = [{"order_amount": "bad"}]
        run = cs.execute_dataset_cleanse("ods_orders", rows, business_date="2026-11-18")
        assert run["records"][0]["hit_details"][0]["business_month"] == "2026-11"


# ---------------------------------------------------------------------------
# 4. 中断后只续写未完成部分
# ---------------------------------------------------------------------------
class TestResumeUnfinished:
    def _rows(self):
        return [
            {"order_id": f"O{i}", "order_amount": ("" if i % 2 else "bad"), "dt": "2026-11-01"}
            for i in range(6)
        ]

    def test_interrupt_then_resume_only_processes_remaining(self):
        cs = CleanseRuleService()
        rows = self._rows()
        interrupted = cs.execute_dataset_cleanse(
            "ods_orders", rows, business_date="2026-11-18", stop_after=2)
        assert interrupted["status"] == "中断"
        assert interrupted["processed_rows"] == 2
        partial_hits = interrupted["records"][0]["hit_count"]

        resumed = cs.resume_dataset_cleanse(interrupted["run_id"], rows)
        assert resumed["status"] == "已完成"
        assert resumed["processed_rows"] == 6
        # 续写在原报告基础上累加，报告仍是同一份（run_id 不变）
        assert resumed["run_id"] == interrupted["run_id"]
        assert resumed["records"][0]["hit_count"] >= partial_hits
        # 6 行全部处理：3 空值 + 3 正则替换
        assert resumed["records"][0]["hit_count"] == 6

    def test_resume_completed_run_rejected(self):
        cs = CleanseRuleService()
        run = cs.execute_dataset_cleanse("ods_orders", self._rows(), business_date="2026-11-18")
        with pytest.raises(ValueError):
            cs.resume_dataset_cleanse(run["run_id"], self._rows())

    def test_resume_with_shorter_input_rejected(self):
        cs = CleanseRuleService()
        rows = self._rows()
        interrupted = cs.execute_dataset_cleanse(
            "ods_orders", rows, business_date="2026-11-18", stop_after=4)
        with pytest.raises(ValueError):
            cs.resume_dataset_cleanse(interrupted["run_id"], rows[:2])

    def test_resume_keeps_processed_and_uses_fresh_remaining_input(self):
        cs = CleanseRuleService()
        rows = [
            {"order_id": "O0", "order_amount": "bad", "dt": "2026-11-01"},
            {"order_id": "O1", "order_amount": "100.00", "dt": "2026-11-01"},
            {"order_id": "O2", "order_amount": "200.00", "dt": "2026-11-01"},
        ]
        interrupted = cs.execute_dataset_cleanse(
            "ods_orders", rows, business_date="2026-11-18", stop_after=1)
        # 已处理的第 0 行结果固定为替换值；未处理的第 2 行用新输入里的脏值
        fresh = [dict(rows[0]), dict(rows[1]),
                 {"order_id": "O2", "order_amount": "dirty", "dt": "2026-11-01"}]
        resumed = cs.resume_dataset_cleanse(interrupted["run_id"], fresh)
        out = {r["order_id"]: r["order_amount"] for r in resumed["rows"]}
        assert out["O0"] == "0.00"   # 已处理结果保留
        assert out["O1"] == "100.00"
        assert out["O2"] == "0.00"   # 新输入的脏值在续写阶段被替换


# ---------------------------------------------------------------------------
# 5. 执行顺序与多规则编排
# ---------------------------------------------------------------------------
class TestExecutionOrder:
    def test_sort_rules_stable_by_exec_order(self):
        rules = [
            {"策略编号": "R3", "执行顺序": "3"},
            {"策略编号": "R1", "执行顺序": "1"},
            {"策略编号": "R2", "执行顺序": "2"},
        ]
        assert [r["策略编号"] for r in sort_rules(rules)] == ["R1", "R2", "R3"]

    def test_unparsable_order_goes_last_keeping_relative_order(self):
        rules = [
            {"策略编号": "RX", "执行顺序": ""},
            {"策略编号": "R1", "执行顺序": "1"},
        ]
        ordered = sort_rules(rules)
        assert ordered[0]["策略编号"] == "R1"
        assert ordered[1]["策略编号"] == "RX"

    def test_multi_rule_records_follow_execution_order(self):
        cs = CleanseRuleService()
        cs.create_entry({
            "策略编号": "CLN-ORDER-2", "目标字段": "order_amount", "空值处理": "填充0.00",
            "正则表达式": r"^\d+\.\d{2}$", "替换值": "0.00", "策略版本": "v1",
            "执行顺序": "2", "目标数据集": "ds_multi",
        })
        cs.create_entry({
            "策略编号": "CLN-ORDER-1", "目标字段": "order_amount", "空值处理": "填充0.00",
            "正则表达式": r"^\d+\.\d{2}$", "替换值": "0.00", "策略版本": "v1",
            "执行顺序": "1", "目标数据集": "ds_multi",
        })
        run = cs.execute_dataset_cleanse(
            "ds_multi", [{"order_amount": "bad"}], business_date="2026-11-18")
        assert [r["rule_no"] for r in run["records"]] == ["CLN-ORDER-1", "CLN-ORDER-2"]


# ---------------------------------------------------------------------------
# 6. 历史/当前版本冲突：原顺序与命中数保留 + 差异清单具体程度排序
# ---------------------------------------------------------------------------
class TestVersionConflict:
    def test_conflict_diff_sorted_by_specificity_and_keeps_baseline(self):
        cs = CleanseRuleService()
        # 同执行顺序位置两条规则，模拟历史版本与当前版本冲突
        cs.create_entry({
            "策略编号": "CLN-CONF-A", "目标字段": "order_amount", "空值处理": "填充0.00",
            "正则表达式": r"^\d+\.\d{2}$", "替换值": "0.00", "策略版本": "v1",
            "执行顺序": "1", "目标数据集": "ds_conf",
        })
        cs.create_entry({
            "策略编号": "CLN-CONF-B", "目标字段": "order_amount", "空值处理": "填充为1",
            "正则表达式": r"^\d+$", "替换值": "9.99", "策略版本": "v2",
            "执行顺序": "1", "目标数据集": "ds_conf",
        })
        run = cs.execute_dataset_cleanse(
            "ds_conf", [{"order_amount": "bad"}], business_date="2026-11-18")
        assert len(run["conflicts"]) == 1
        conflict = run["conflicts"][0]
        # 优先级尚待确认，不擅自改写执行顺序
        assert conflict["priority"] == "待确认"
        fields = [c["field"] for c in conflict["changed_fields"]]
        # 具体程度排序：regex(0) -> replace_value(1) -> null_handle(2) -> version(6)
        assert fields == sorted(fields, key=lambda f: next(
            c["specificity_rank"] for c in conflict["changed_fields"] if c["field"] == f))
        assert fields.index("regex") < fields.index("replace_value")
        assert fields.index("replace_value") < fields.index("null_handle")
        # 报告里规则仍按原始稳定顺序，命中数照常统计（原版本口径保留）
        assert [r["rule_no"] for r in run["records"]] == ["CLN-CONF-A", "CLN-CONF-B"]


# ---------------------------------------------------------------------------
# 7. 质量稽核跨模块回写报告，通过率口径不变
# ---------------------------------------------------------------------------
class TestQualityWriteback:
    def test_quality_result_written_back_to_latest_report(self):
        cs = CleanseRuleService()
        qc = QualityCheckService()
        rows = [
            {"order_id": "O1", "order_amount": "100.00", "dt": "2026-11-01"},
            {"order_id": "O2", "order_amount": "-1.00", "dt": "2026-11-01"},
        ]
        run = cs.execute_dataset_cleanse("ods_orders", rows, business_date="2026-11-18")
        # 稽核直接读 ods_orders 模拟表
        store.set_table_rows("ods_orders", rows)
        qc_result = qc.execute_check(1)
        # 稽核通过率/失败数口径不变
        assert qc_result["result"]["failed"] == 1
        assert qc_result["result"]["passed"] == 1

        refreshed = cs.get_report(run["run_id"])
        writebacks = refreshed["quality_writebacks"]
        assert len(writebacks) == 1
        wb = writebacks[0]
        assert wb["quality_rule_no"] == "QC-ORD-001"
        assert wb["failed"] == 1 and wb["passed"] == 1
        assert wb["pass_rate"] == 0.5

    def test_quality_without_report_does_not_break(self):
        qc = QualityCheckService()
        store.set_table_rows("ods_orders", [{"order_amount": "1.00", "dt": "2026-11-01"}])
        result = qc.execute_check(1)  # 无报告时不报错
        assert result["ok"] is True
