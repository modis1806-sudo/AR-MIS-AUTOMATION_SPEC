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
 * Search matches the ENTIRE row's visible text, case-insensitive - the
 * client's own ask was simply "let me find the party in the list",
 * not a column-scoped search. Column filters are opt-in dropdowns,
 * auto-populated from whatever values actually appear in that column
 * (never a hardcoded list, so it never drifts from the real data).
 *
 * A live-sum target anywhere on the page (typically a KPI tile) can
 * declare data-tt-sum="UNIQUE_ID:col_name" and its text is replaced
 * with the Indian-grouped sum of data-value on every currently VISIBLE
 * row's matching column - the client's own "just like SUMIFS" ask.
 *
 * Select-all only ever acts on currently visible rows (same as Gmail's
 * own select-all-in-this-view behavior, which the client named
 * explicitly) - filtering first, then selecting, is the whole point.
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

  function initTable(container) {
    var id = container.getAttribute("data-tt");
    var table = document.querySelector('table[data-tt-table="' + id + '"]');
    if (!table) return;

    var searchInput = container.querySelector("[data-tt-search]");
    var filtersHost = container.querySelector("[data-tt-filters]");
    var clearBtn = container.querySelector("[data-tt-clear]");
    var countEl = container.querySelector("[data-tt-count]");
    var selectedCountEl = container.querySelector("[data-tt-selected-count]");

    var headerRow = table.querySelector("thead tr");
    var rows = Array.prototype.slice.call(table.querySelectorAll("tbody tr[data-tt-row]"));
    var selects = [];

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
          if (cell) values[cell.textContent.trim()] = true;
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
      var activeFilters = selects.map(function (s) {
        return { col: s.getAttribute("data-tt-filter-col"), value: s.value };
      });
      var visible = 0;
      rows.forEach(function (row) {
        var matches = true;
        if (term && row.textContent.toLowerCase().indexOf(term) === -1) {
          matches = false;
        }
        if (matches) {
          for (var i = 0; i < activeFilters.length; i++) {
            var f = activeFilters[i];
            if (!f.value) continue;
            var cell = row.querySelector('td[data-tt-col="' + f.col + '"]');
            if (!cell || cell.textContent.trim() !== f.value) {
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

    applyFilters();
  }

  document.addEventListener("DOMContentLoaded", function () {
    Array.prototype.forEach.call(document.querySelectorAll("[data-tt]"), initTable);
  });
})();
