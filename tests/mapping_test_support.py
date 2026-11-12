"""Exercise the real native mapping confirmation in existing business tests."""
from xiaoyu.cloud_mapping import mapping_answer_text


def confirm_columns(cloud, snapshot, columns):
    saved = cloud.snapshots[snapshot["snapshot_id"]]
    if cloud._mapping_requirement(saved, columns) is None:
        return snapshot
    if cloud._last_completed:
        cloud.begin_turn("按原规则重新更新当前云表")
        overrides = [{"name": name, "column": col} for name, col in saved["columns"].items()
            if any(h["column"] == col and h.get("merge_range") for h in saved["expected_headers"])]
        snapshot = cloud.execute("wps_read_table", dict(document_url=saved["document_url"], sheet=saved["sheet"],
            columns=list(saved["columns"]), header_row=saved["header_row"], start_row=saved["row_start"],
            end_row=saved["row_end"], column_overrides=overrides or None))
        assert snapshot["ok"], snapshot
        saved = cloud.snapshots[snapshot["snapshot_id"]]
    question = "请逐行选择测试来源列"
    pairs = [(item["target_column"], item["source_field"]) for item in columns]
    result = cloud.execute("wps_confirm_mapping", dict(document_url=saved["document_url"],
        sheet=saved["sheet"], question=question, mappings=[dict(target_column=target,
            source_options=[source], selected_source=None, note="合成来源数据") for target, source in pairs]))
    assert result["ok"], result
    cloud.begin_turn(f"针对“{question}”：" + mapping_answer_text(saved["sheet"], pairs),
                     force_preview=cloud._force_preview)
    assert cloud.get_pending_mapping_question() is None
    return snapshot
