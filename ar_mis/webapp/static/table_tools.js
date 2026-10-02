/* table_tools.js - shared search / filter / live-sum / select-all
 * behavior for every register and report table in this app.
 *
 * No dependencies, no build step - one small vanilla-JS file, applied
 * to any table purely through data-tt-* attributes in the template, so
 * each screen just marks up its own table instead of writing its own
 * script. Contract:
 *
 *   <div class="table-tools" data-tt="UNIQUE_ID">
 *     <input type="search" data-tt-search>
 *     <select data-tt-search-scope></select>  (optional - see below)
 *     <span data-tt-filters></span>          (dropdowns injected here)
 *     <button type="button" data-tt-clear>Clear filters</button>
 *     <span data-tt-count></span>
 *     <span data-tt-selected-count></span>   (optional, only if selectable)
 *   </div>
 *   <table data-tt-table="UNIQUE_ID">
 *     <thead><tr>
 *       <th data-tt-filter data-tt-col="branch">Branch</th>   (dropdown)
 *       <th>Some other column</th>                             (search-only)
 *     </tr></thead>
 *     <tbody>
 *       <tr data-tt-row>
 *         <td data-tt-col="branch">KOL</td>
 *         <td data-tt-col="closing_tb" data-value="730.00">730.00</td>
 *       </tr>
 *     </tbody>
 *   </table>
 *
 * Search matches the ENTIRE row's visible text by default, case-
 * insensitive. Client's own later catch, after a whole-row match swept
 * an unintended row into a batch financial action (searching "25-26"
 * to mean "target invoices from that FY" also matched a voucher whose
 * OWN number happened to carry the same digits): an optional
 * data-tt-search-scope <select> is auto-populated with one option per
 * table column (by header label) plus "All columns", letting the exact
 * same search box be pinned to a single column - Excel's own "search
 * this column" behavior - without needing a text box per column, which
 * would turn a 15+ column register header into clutter. Column filters
 * are a separate, opt-in dropdown of exact values, auto-populated from
 * whatever actually appears in that column (never a hardcoded list, so
 * it never drifts from the real data).
 *
 * A live-sum target anywhere on the page (typically a KPI tile) can
 * declare data-tt-sum="UNIQUE_ID:col_name" and its text is replaced
 * with the Indian-grouped sum of data-value on every currently VISIBLE
 * row's matching column - the client's own "just like SUMIFS" ask.
 *
 * Select-all only ever acts on currently visible rows (same as Gmail's
 * own select-all-in-this-view behavior, which the client named
 * explicitly) - filtering first, then selecting, is the whole point.
 *
 * Any <form> that a [data-tt-row-select] checkbox targets (via its own
 * form="..." attribute - the batch-action forms on every register) gets
 * a confirmation step wired on automatically: submitting it first shows
 * exactly which rows (read from each row's own [data-tt-row-label] cell)
 * and what field values are about to be applied, and only proceeds if
 * the user confirms. Same root cause as the search-scope feature above -
 * a batch action with real financial consequences must never commit to
 * a selection the user hasn't actually seen confirmed, regardless of
 * how a row ended up checked (a loose search match, a leftover
 * selection, a misclick).
 */
(function () {
  "use strict";

  function formatINR(value) {
    var negative = value < 0;
    var abs = Math.abs(value);
    var fixed = abs.toFixed(2);
    var dot = fixed.indexOf(".");
    var intPart = fixed.slice(0, dot);
    var fracPart = fixed.slice(dot + 1);
    var grouped;
    if (intPart.length <= 3) {
      grouped = intPart;
    } else {
      var lastThree = intPart.slice(-3);
      var rest = intPart.slice(0, -3);
      var groups = [];
      while (rest.length > 2) {
        groups.unshift(rest.slice(-2));
        rest = rest.slice(0, -2);
      }
      if (rest) groups.unshift(rest);
      grouped = groups.join(",") + "," + lastThree;
    }
    return (negative ? "-" : "") + grouped + "." + fracPart;
  }

  // A data-tt-col cell that holds a live-editable input (PTP Amount,
  // Next Action, etc.) must be read/filtered/searched by what's actually
  // typed in that input right now, not the cell's static textContent -
  // the server only ever rendered the saved value into the input's own
  // value attribute, so textContent of the cell is empty or (worse, for
  // a cell that also holds a quick-pick <select>) full of every preset
  // option's label instead of the real current value.
  //
  // A cell can also hold an inline action form of its own (the CN/Receipt
  // Classification column's "Resolve" details/select/button, for a
  // Pending Review row) with no input box to key off at all - live-caught
  // regression: once the Resolve form's old Reason text box was removed,
  // cellText() fell through to raw textContent and the filter dropdown
  // started listing the whole hidden form (its select options, its Save
  // button) as one garbled "option". data-tt-filter-value is the escape
  // hatch for exactly this: a cell says explicitly what it means for
  // filtering, instead of this function trying to guess it out of
  // arbitrary markup.
  function cellText(cell) {
    var filterValue = cell.getAttribute("data-tt-filter-value");
    if (filterValue !== null) return filterValue.trim();
    var input = cell.querySelector('input[type="text"], input[type="number"], input[type="date"]');
    if (input) return input.value.trim();
    // Falls through here for a cell with a <select> but no input of its
    // own (e.g. the Classification column's hidden Resolve form on a
    // Pending Review row, before it ever gets a data-tt-filter-value) -
    // stripped for the same reason rowSearchText strips it below: a
    // <select>'s own option labels are boilerplate, not this row's data.
    var clone = cell.cloneNode(true);
    Array.prototype.forEach.call(clone.querySelectorAll("select"), function (s) {
      s.parentNode.removeChild(s);
    });
    return clone.textContent.trim();
  }

  // Same problem at the whole-row level for the free-text search box:
  // row.textContent would (a) miss every input's current value entirely
  // and (b) for a row containing a quick-pick <select>, incorrectly
  // match on every one of that select's option labels regardless of
  // what's actually selected or typed - a search for "customer" would
  // hit every single row once a Next Action dropdown existed, because
  // "Call customer" sits in its option list whether or not anyone chose
  // it. Select elements are excluded entirely; their own sibling input
  // (if any) already carries the real value and is included below.
  function rowSearchText(row) {
    var clone = row.cloneNode(true);
    Array.prototype.forEach.call(clone.querySelectorAll("select"), function (s) {
      s.parentNode.removeChild(s);
    });
    var text = clone.textContent || "";
    Array.prototype.forEach.call(row.querySelectorAll('input[type="text"], input[type="number"]'), function (el) {
      text += " " + (el.value || "");
    });
    return text.toLowerCase();
  }

  function initTable(container) {
    var id = container.getAttribute("data-tt");
    var table = document.querySelector('table[data-tt-table="' + id + '"]');
    if (!table) return;

    var searchInput = container.querySelector("[data-tt-search]");
    var searchScope = container.querySelector("[data-tt-search-scope]");
    var filtersHost = container.querySelector("[data-tt-filters]");
    var clearBtn = container.querySelector("[data-tt-clear]");
    var countEl = container.querySelector("[data-tt-count]");
    var selectedCountEl = container.querySelector("[data-tt-selected-count]");

    var headerRow = table.querySelector("thead tr");
    var rows = Array.prototype.slice.call(table.querySelectorAll("tbody tr[data-tt-row]"));
    var selects = [];

    // Excel's own "search this column" - one option per header label,
    // by cell position so it works whether or not that column also
    // carries a data-tt-col (most do, for filters/sums; plain text
    // columns like Date or Voucher No. often don't). A header with no
    // text (the select-all checkbox column) is skipped - nothing to
    // search there.
    if (searchScope && headerRow) {
      var allColumnsOpt = document.createElement("option");
      allColumnsOpt.value = "";
      allColumnsOpt.textContent = "All columns";
      searchScope.appendChild(allColumnsOpt);
      Array.prototype.forEach.call(headerRow.children, function (th, idx) {
        var label = th.textContent.trim();
        if (!label) return;
        var opt = document.createElement("option");
        opt.value = String(idx);
        opt.textContent = label;
        searchScope.appendChild(opt);
      });
      searchScope.addEventListener("change", applyFilters);
    }

    if (headerRow && filtersHost) {
      var filterHeaders = Array.prototype.slice.call(
        headerRow.querySelectorAll("th[data-tt-filter]")
      );
      filterHeaders.forEach(function (th) {
        var col = th.getAttribute("data-tt-col");
        var label = th.textContent.trim();
        var values = {};
        rows.forEach(function (row) {
          var cell = row.querySelector('td[data-tt-col="' + col + '"]');
          // A blank value (e.g. no Next Action set yet) is skipped here -
          // the dropdown's own "All ..." option already covers "show
          // everything", so a second, unlabeled blank entry would just
          // be a confusing duplicate of it.
          if (cell) {
            var v = cellText(cell);
            if (v) values[v] = true;
          }
        });
        var sorted = Object.keys(values).sort();
        var select = document.createElement("select");
        select.setAttribute("data-tt-filter-col", col);
        var allOpt = document.createElement("option");
        allOpt.value = "";
        allOpt.textContent = "All " + label;
        select.appendChild(allOpt);
        sorted.forEach(function (v) {
          var opt = document.createElement("option");
          opt.value = v;
          opt.textContent = v;
          select.appendChild(opt);
        });
        select.addEventListener("change", applyFilters);
        filtersHost.appendChild(select);
        selects.push(select);
      });
    }

    function updateSums() {
      var sumEls = document.querySelectorAll('[data-tt-sum^="' + id + ':"]');
      Array.prototype.forEach.call(sumEls, function (el) {
        var col = el.getAttribute("data-tt-sum").split(":")[1];
        var total = 0;
        rows.forEach(function (row) {
          if (row.style.display === "none") return;
          var cell = row.querySelector('td[data-tt-col="' + col + '"]');
          if (!cell) return;
          var raw = cell.getAttribute("data-value");
          if (raw === null) return;
          var n = parseFloat(raw);
          if (!isNaN(n)) total += n;
        });
        el.textContent = formatINR(total);
      });
    }

    function updateSelectedCount() {
      if (!selectedCountEl) return;
      var checked = table.querySelectorAll("[data-tt-row-select]:checked").length;
      selectedCountEl.textContent = checked + " selected";
    }

    function applyFilters() {
      var term = searchInput ? searchInput.value.trim().toLowerCase() : "";
      // "" (the default "All columns" option) keeps the existing
      // whole-row search; any other value pins the box to that one
      // column index instead.
      var scopeIndex = searchScope && searchScope.value !== "" ? parseInt(searchScope.value, 10) : -1;
      var activeFilters = selects.map(function (s) {
        return { col: s.getAttribute("data-tt-filter-col"), value: s.value };
      });
      var visible = 0;
      rows.forEach(function (row) {
        var matches = true;
        if (term) {
          if (scopeIndex === -1) {
            if (rowSearchText(row).indexOf(term) === -1) matches = false;
          } else {
            var scopedCell = row.children[scopeIndex];
            if (!scopedCell || cellText(scopedCell).toLowerCase().indexOf(term) === -1) matches = false;
          }
        }
        if (matches) {
          for (var i = 0; i < activeFilters.length; i++) {
            var f = activeFilters[i];
            if (!f.value) continue;
            var cell = row.querySelector('td[data-tt-col="' + f.col + '"]');
            if (!cell || cellText(cell) !== f.value) {
              matches = false;
              break;
            }
          }
        }
        row.style.display = matches ? "" : "none";
        if (matches) visible++;
        if (!matches) {
          var checkbox = row.querySelector("[data-tt-row-select]");
          if (checkbox) checkbox.checked = false;
        }
      });
      if (countEl) countEl.textContent = "Showing " + visible + " of " + rows.length;
      updateSums();
      updateSelectedCount();
    }

    if (searchInput) searchInput.addEventListener("input", applyFilters);
    if (clearBtn) {
      clearBtn.addEventListener("click", function () {
        if (searchInput) searchInput.value = "";
        if (searchScope) searchScope.value = "";
        selects.forEach(function (s) {
          s.value = "";
        });
        applyFilters();
      });
    }

    var selectAll = table.querySelector("[data-tt-select-all]");
    if (selectAll) {
      selectAll.addEventListener("change", function () {
        rows.forEach(function (row) {
          if (row.style.display === "none") return;
          var checkbox = row.querySelector("[data-tt-row-select]");
          if (checkbox) checkbox.checked = selectAll.checked;
        });
        updateSelectedCount();
      });
    }
    Array.prototype.forEach.call(table.querySelectorAll("[data-tt-row-select]"), function (cb) {
      cb.addEventListener("change", updateSelectedCount);
    });

    // An editable numeric cell (e.g. PTP Amount) only had its data-value
    // set once, from the server-rendered page - typing a new figure into
    // the input never touched that attribute, so the live SUMIFS-style
    // total above the table silently ignored every unsaved edit. Any
    // number input living directly inside a data-tt-col cell now keeps
    // that cell's data-value in sync on every keystroke, so the total
    // reflects what's actually in the box right now, not just what was
    // last saved to the database.
    Array.prototype.forEach.call(
      table.querySelectorAll('td[data-tt-col] > input[type="number"]'),
      function (input) {
        var cell = input.parentElement;
        input.addEventListener("input", function () {
          var n = parseFloat(input.value);
          if (isNaN(n)) {
            cell.removeAttribute("data-value");
          } else {
            cell.setAttribute("data-value", input.value);
          }
          updateSums();
        });
      }
    );

    // Client's own ask, after a loose search match swept an unintended
    // row into a batch Pre-MIS resolve: every batch-action form (any
    // <form> a [data-tt-row-select] checkbox targets via its own
    // form="..." attribute) gets a confirmation step wired on
    // automatically, no per-template markup needed beyond giving each
    // row a [data-tt-row-label] cell to identify it by. Submitting the
    // form first lists exactly which rows and field values are about to
    // be applied and requires an explicit OK - this catches a wrong
    // selection regardless of how it happened (a loose search match, a
    // leftover checkbox, a misclick), which column-scoped search above
    // helps prevent but can't fully rule out on its own.
    var batchFormIds = {};
    Array.prototype.forEach.call(table.querySelectorAll("[data-tt-row-select]"), function (cb) {
      var formId = cb.getAttribute("form");
      if (formId) batchFormIds[formId] = true;
    });
    Object.keys(batchFormIds).forEach(function (formId) {
      var form = document.getElementById(formId);
      if (!form) return;
      form.addEventListener("submit", function (evt) {
        var checked = Array.prototype.slice.call(table.querySelectorAll('[data-tt-row-select]:checked'));
        if (checked.length === 0) return; // nothing selected - the server's own message handles this
        var labels = checked.map(function (cb) {
          var row = cb.closest("tr[data-tt-row]");
          var labelCell = row && row.querySelector("[data-tt-row-label]");
          return labelCell ? cellText(labelCell) : "(unlabeled row)";
        });
        var fieldSummary = [];
        Array.prototype.forEach.call(form.querySelectorAll("input, select"), function (el) {
          if (el.type === "hidden" || el.type === "checkbox" || el.name === "selected" || !el.value) return;
          fieldSummary.push(el.name + ": " + el.value);
        });
        var shown = labels.slice(0, 20);
        var message = "Apply to these " + labels.length + " selected row(s)?\n";
        if (fieldSummary.length) message += "\n" + fieldSummary.join("\n") + "\n";
        message += "\n" + shown.join("\n");
        if (labels.length > shown.length) {
          message += "\n...and " + (labels.length - shown.length) + " more";
        }
        if (!window.confirm(message)) {
          evt.preventDefault();
        }
      });
    });

    applyFilters();
  }

  document.addEventListener("DOMContentLoaded", function () {
    Array.prototype.forEach.call(document.querySelectorAll("[data-tt]"), initTable);
  });
})();
