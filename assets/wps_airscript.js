/*
 * 给小雨的软件 — WPS AirScript 2.0 文档共享脚本。
 * 将本文件完整复制到目标文档的共享脚本，保存后绑定 sync_task webhook。
 * 仅提供读取、按业务编号定位及单列条件写入，不执行调用方提供的代码。
 * 批次不是事务：保存/网络失败可能已部分修改，调用方必须记账并独立回读。
 */
function runXiaoyuWps() {
  const result = {
    ok: false, protocol: "xiaoyu-wps/1", action: "", workbook_name: "",
    mutation_attempted: false
  };
  function fail(code) { result.ok = false; result.code = code; return result; }
  function invalid() { throw new Error("INVALID_ARGUMENTS"); }
  function own(obj, key) { return Object.prototype.hasOwnProperty.call(obj, key); }
  function object(value) { return value !== null && typeof value === "object" && !Array.isArray(value); }
  function integer(value, low, high) { return typeof value === "number" && isFinite(value) && Math.floor(value) === value && value >= low && value <= high; }
  function scalar(value) { return value === null || typeof value === "boolean" || (typeof value === "string" && value.length <= 32767) || (typeof value === "number" && isFinite(value)); }
  function normalized(value) { return value === null ? "" : value; }
  function same(a, b) { return typeof a === typeof b && a === b; }
  function exactKeys(value, keys) { return object(value) && Object.keys(value).length === keys.length && keys.every(function (key) { return own(value, key); }); }
  function uniqueColumns(value) {
    if (!Array.isArray(value) || !value.length || value.length > 30) return false;
    const seen = {};
    for (let i = 0; i < value.length; i++) {
      if (!integer(value[i], 1, 16384) || seen[value[i]]) return false;
      seen[value[i]] = true;
    }
    return true;
  }
  function colName(column) {
    let name = "";
    while (column > 0) { column--; name = String.fromCharCode(65 + column % 26) + name; column = Math.floor(column / 26); }
    return name;
  }
  function cellAt(sheet, row, column) { return sheet.Range(colName(column) + row); }
  function snapshot(cell) {
    let value = cell.Value2;
    const formula = cell.Formula;
    const numberFormat = cell.NumberFormat;
    const hasFormula = cell.HasFormula;
    const merged = cell.MergeCells;
    const count = cell.Count;
    // An absent Value2 alone never proves a blank, notably for merged cells
    // and formulas whose calculated result is an empty string.
    if (value === undefined && formula === "" && hasFormula === false &&
        merged === false && count === 1 && Application.WorksheetFunction.CountBlank(cell) === 1) value = "";
    if (!scalar(value) || !scalar(formula) || typeof numberFormat !== "string" ||
        typeof hasFormula !== "boolean" || typeof merged !== "boolean" || count !== 1) {
      throw new Error("UNSUPPORTED_CELL_METADATA");
    }
    return {value: normalized(value), formula: normalized(formula), number_format: numberFormat,
            has_formula: hasFormula, merged: merged};
  }
  function readSnapshot(cell) {
    if (cell.MergeCells !== true) return snapshot(cell);
    // Merged title children may expose no Value2/Formula/format in AirScript
    // 2.0. They are display-only placeholders, NEVER evidence of an editable
    // blank. Keep merged=true and treat an unknown formula flag as protected.
    // This tolerant path is called ONLY by read, never by apply/key/guards.
    function optional(getter) { try { return getter(); } catch (error) { return undefined; } }
    const value = optional(function () { return cell.Value2; });
    const formula = optional(function () { return cell.Formula; });
    const format = optional(function () { return cell.NumberFormat; });
    const hasFormula = optional(function () { return cell.HasFormula; });
    return {value: scalar(value) ? normalized(value) : "",
            formula: scalar(formula) ? normalized(formula) : "",
            number_format: typeof format === "string" ? format : "",
            has_formula: typeof hasFormula === "boolean" ? hasFormula : true,
            merged: true};
  }
  function getSheet(name) {
    let sheet;
    try { sheet = Application.Worksheets.Item(name); } catch (error) { throw new Error("SHEET_NOT_FOUND"); }
    if (!sheet || sheet.Name !== name) throw new Error("SHEET_NOT_FOUND");
    return sheet;
  }
  function keyToken(values) { return JSON.stringify(values); }
  function readKeys(sheet, row, columns) {
    const values = [];
    for (let i = 0; i < columns.length; i++) {
      const cell = cellAt(sheet, row, columns[i]);
      // Merged identifiers cannot identify an individual business row.
      if (cell.MergeCells !== false) return null;
      const item = snapshot(cell);
      if (item.value === "" || item.value === null) return null;
      values.push(item.value);
    }
    return values;
  }
  function validateApply(args) {
    if (!integer(args.row_start, 1, 100000) || !integer(args.row_end, args.row_start, 100000) ||
        !integer(args.header_row, 1, args.row_start - 1) || !integer(args.target_column, 1, 16384) ||
        !uniqueColumns(args.key_columns) || !Array.isArray(args.expected_headers) ||
        args.expected_headers.length < 1 || args.expected_headers.length > 60 ||
        !Array.isArray(args.changes) || !args.changes.length || args.changes.length > 100 ||
        typeof args.task_id !== "string" || !/^[A-Za-z0-9_-]{1,128}$/.test(args.task_id) ||
        typeof args.batch_id !== "string" || !/^[A-Za-z0-9_-]{1,128}$/.test(args.batch_id)) invalid();
    const headers = {};
    for (let i = 0; i < args.expected_headers.length; i++) {
      const h = args.expected_headers[i];
      if (!exactKeys(h, ["column", "value"]) || !integer(h.column, 1, 16384) || !scalar(h.value) || headers[h.column]) invalid();
      headers[h.column] = true;
    }
    if (!headers[args.target_column] || !args.key_columns.every(function (c) { return headers[c]; })) invalid();
    for (let i = 0; i < args.changes.length; i++) {
      const c = args.changes[i];
      if (!exactKeys(c, ["row", "key_values", "old_value", "old_formula", "old_number_format", "new_value", "guards"]) ||
          !integer(c.row, args.row_start, args.row_end) || !Array.isArray(c.key_values) ||
          c.key_values.length !== args.key_columns.length || !c.key_values.every(function (v) { return scalar(v) && v !== null && v !== ""; }) ||
          !scalar(c.old_value) || !scalar(c.old_formula) || !scalar(c.new_value) ||
          typeof c.old_number_format !== "string" || c.old_number_format.length > 1024 ||
          !Array.isArray(c.guards) || c.guards.length > 30) invalid();
      if (typeof c.new_value === "string" && c.new_value.charAt(0) === "=") throw new Error("INVALID_NEW_VALUE");
      const seen = {};
      for (let j = 0; j < c.guards.length; j++) {
        const g = c.guards[j];
        if (!exactKeys(g, ["column", "value", "formula"]) || !integer(g.column, 1, 16384) ||
            !scalar(g.value) || !scalar(g.formula) || !headers[g.column] || seen[g.column]) invalid();
        seen[g.column] = true;
      }
    }
  }
  try {
    const args = typeof Context !== "undefined" ? Context.argv : null;
    if (!object(args)) return fail("INVALID_ARGUMENTS");
    const action = args.action;
    if (action !== "describe" && action !== "read" && action !== "apply") return fail("INVALID_ACTION");
    result.action = action;
    if (args.protocol !== result.protocol) return fail("INVALID_PROTOCOL");
    if (typeof args.expected_workbook_name !== "string" || args.expected_workbook_name.length > 512) return fail("INVALID_ARGUMENTS");
    if (!args.expected_workbook_name && action !== "describe") return fail("NAME_REQUIRED");
    // Runtime-injected Context fields are deliberately ignored. Only explicitly
    // validated fields can influence ranges and operations.
    const workbook = Application.ActiveWorkbook;
    if (!workbook || typeof workbook.Name !== "string" || !workbook.Name || workbook.Name.length > 512) return fail("UNSUPPORTED_DOCUMENT_METADATA");
    result.workbook_name = workbook.Name;
    if (args.expected_workbook_name && args.expected_workbook_name !== workbook.Name) return fail("WORKBOOK_MISMATCH");
    if (action === "describe") {
      const count = Application.Worksheets.Count;
      if (!integer(count, 0, 1024)) return fail("UNSUPPORTED_DOCUMENT_METADATA");
      result.sheets = [];
      for (let i = 1; i <= count; i++) {
        const sheet = Application.Worksheets.Item(i);
        const used = sheet.UsedRange;
        const row = used.Row, column = used.Column;
        const rowCount = used.Rows.Count, columnCount = used.Columns.Count;
        if (typeof sheet.Name !== "string" || !sheet.Name || !integer(row, 1, 1048576) ||
            !integer(column, 1, 16384) || !integer(rowCount, 1, 1048576) || !integer(columnCount, 1, 16384) ||
            row + rowCount - 1 > 1048576 || column + columnCount - 1 > 16384) return fail("UNSUPPORTED_DOCUMENT_METADATA");
        result.sheets.push({name: sheet.Name, used_start_row: row, used_end_row: row + rowCount - 1,
                           used_start_column: column, used_end_column: column + columnCount - 1});
      }
      result.ok = true;
      return result;
    }
    if (typeof args.sheet !== "string" || !args.sheet || args.sheet.length > 255) return fail("INVALID_ARGUMENTS");
    const sheet = getSheet(args.sheet);
    if (action === "read") {
      if (!integer(args.start_row, 1, 100000) || !integer(args.end_row, args.start_row, 100000) ||
          args.end_row - args.start_row >= 500 || !uniqueColumns(args.columns)) return fail("INVALID_ARGUMENTS");
      result.rows = [];
      for (let row = args.start_row; row <= args.end_row; row++) {
        const cells = [];
        for (let j = 0; j < args.columns.length; j++) {
          const column = args.columns[j];
          const entry = readSnapshot(cellAt(sheet, row, column));
          entry.column = column;
          cells.push(entry);
        }
        result.rows.push({row: row, cells: cells});
      }
      result.ok = true;
      return result;
    }
    validateApply(args); // Validate the entire batch before any mutation.
    result.task_id = args.task_id;
    result.batch_id = args.batch_id;
    result.outcomes = [];
    for (let h = 0; h < args.expected_headers.length; h++) {
      const expected = args.expected_headers[h];
      const current = snapshot(cellAt(sheet, args.header_row, expected.column));
      if (current.merged || !same(current.value, expected.value)) return fail("HEADER_MISMATCH");
    }
    const requested = Object.create(null), positions = Object.create(null);
    for (let i = 0; i < args.changes.length; i++) {
      const token = keyToken(args.changes[i].key_values);
      requested[token] = (requested[token] || 0) + 1;
      positions[token] = [];
    }
    // Search only the authorised inclusive range. Row numbers are hints; the
    // business key, its uniqueness, and old values determine whether to write.
    for (let row = args.row_start; row <= args.row_end; row++) {
      const values = readKeys(sheet, row, args.key_columns);
      if (values === null) continue;
      const token = keyToken(values);
      if (own(positions, token)) positions[token].push(row);
    }
    for (let i = 0; i < args.changes.length; i++) {
      const change = args.changes[i];
      const token = keyToken(change.key_values), matches = positions[token];
      const outcome = {index: i, status: "conflict", row: change.row, column: args.target_column};
      result.outcomes.push(outcome);
      if (!matches.length) { outcome.status = "not_found"; continue; }
      if (matches.length !== 1 || requested[token] !== 1) { outcome.status = "duplicate"; continue; }
      const row = matches[0];
      outcome.row = row;
      const cell = cellAt(sheet, row, args.target_column);
      if (cell.MergeCells === true) { outcome.status = "merged"; continue; }
      const current = snapshot(cell);
      if (current.has_formula) { outcome.status = "formula"; continue; }
      const latestKeys = readKeys(sheet, row, args.key_columns);
      if (latestKeys === null || keyToken(latestKeys) !== token ||
          !same(current.value, change.old_value) || !same(current.formula, change.old_formula) ||
          current.number_format !== change.old_number_format) continue;
      let guardsMatch = true;
      for (let j = 0; j < change.guards.length; j++) {
        const guard = change.guards[j], checkCell = cellAt(sheet, row, guard.column);
        if (checkCell.MergeCells !== false) { guardsMatch = false; break; }
        const check = snapshot(checkCell);
        if (!same(check.value, guard.value) || !same(check.formula, guard.formula)) { guardsMatch = false; break; }
      }
      if (!guardsMatch) continue;
      const newValue = normalized(change.new_value);
      if (same(current.value, newValue)) { outcome.status = "unchanged"; continue; }
      result.mutation_attempted = true; // Set BEFORE the first potentially mutating API.
      if (newValue === "") cell.ClearContents();
      else if (typeof newValue === "string" && current.number_format !== "@") {
        // General/date/number formats may parse an input string. Enter strings
        // under the text format first so identifiers like "00123" and literal
        // dates remain text. Changing the format back does not re-enter Value2.
        // Always attempt restoration, including when the value setter throws.
        try {
          cell.NumberFormat = "@";
          if (cell.NumberFormat !== "@") throw new Error("WRITE_VERIFY_FAILED");
          cell.Value2 = newValue;
        } finally {
          cell.NumberFormat = current.number_format;
        }
      }
      else cell.Value2 = newValue;
      const after = snapshot(cell);
      if (!same(after.value, newValue) || after.has_formula || after.merged || after.number_format !== current.number_format) return fail("WRITE_VERIFY_FAILED");
      outcome.status = "applied";
    }
    // Save only once. A failure must retain mutation_attempted and outcomes so
    // the caller can reconcile partial/unknown results using a separate read.
    if (result.mutation_attempted) workbook.Save();
    result.ok = true;
    return result;
  } catch (error) {
    const known = ["INVALID_ARGUMENTS", "INVALID_NEW_VALUE", "SHEET_NOT_FOUND", "UNSUPPORTED_CELL_METADATA", "WRITE_VERIFY_FAILED"];
    const code = error && known.indexOf(error.message) >= 0 ? error.message : "API_ERROR";
    return fail(code); // Never expose native exceptions, credentials or raw logs.
  }
}

return runXiaoyuWps();
