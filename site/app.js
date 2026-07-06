(function () {
  "use strict";

  function statusBadge(value) {
    if (!value) return "";
    var className = "badge-" + value.replace(/\s+/g, "-");
    return '<span class="badge ' + className + '">' + value + "</span>";
  }

  function releaseByBadge(value) {
    if (!value) return "";
    return '<span class="badge tag-' + value + '">' + value + "</span>";
  }

  function prLink(pr) {
    if (!pr) return "";
    return '<a href="' + pr.url + '" target="_blank" rel="noopener">#' + pr.number + "</a>";
  }

  function plain(value) {
    return value == null ? "" : String(value);
  }

  function formatTimestamp(iso) {
    if (!iso) return "—";
    try {
      var d = new Date(iso);
      var parts = new Intl.DateTimeFormat("en-GB", {
        timeZone: "Asia/Singapore",
        weekday: "short",
        day: "2-digit",
        month: "short",
        year: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      }).format(d);
      return parts + " UTC+8";
    } catch (e) {
      return iso;
    }
  }

  function populateFilter(select, values) {
    var sorted = Array.from(new Set(values.filter(Boolean))).sort();
    sorted.forEach(function (v) {
      var opt = document.createElement("option");
      opt.value = v;
      opt.textContent = v;
      select.appendChild(opt);
    });
  }

  function renderPlanner(p) {
    var el = document.getElementById("planner");
    if (!el || !p) return;
    var badge = document.getElementById("planner-badge");
    var phase = p.phase;
    var status, cls;
    if (phase === "pre-start") {
      status = "Starts " + p.startDate + " · " + p.dailyQuota + "/day target";
      cls = "planner-neutral";
    } else if (phase === "complete") {
      status = "🎉 Goal reached — " + p.releasedTotal + "/" + p.totalPackages;
      cls = "planner-ahead";
    } else if (phase === "past-deadline") {
      status = "Deadline passed · " + p.releasedTotal + "/" + p.totalPackages +
        " released · " + p.remaining + " remaining";
      cls = "planner-behind";
    } else {
      var d = p.delta;
      if (d >= 0) {
        status = "✓ On track · " + d + " ahead";
        cls = "planner-ahead";
      } else {
        status = "⚠ Behind by " + (-d) + " package(s)";
        cls = "planner-behind";
      }
    }
    badge.textContent = status;
    badge.className = "planner-badge " + cls;

    document.getElementById("planner-target-today").textContent = p.targetByToday;
    document.getElementById("planner-week-target").textContent = p.targetThisWeekRemaining;
    var weekSub = document.getElementById("planner-week-sub");
    if (weekSub && typeof p.releasedThisWeek === "number" && typeof p.weeklyQuota === "number") {
      weekSub.textContent = "left · " + p.releasedThisWeek + "/" + p.weeklyQuota + " done";
    }
    document.getElementById("planner-pace").textContent = p.requiredPaceToFinish;
    document.getElementById("planner-deadline").textContent = p.deadline;
    el.hidden = false;
  }

  fetch("data-main.json", { cache: "no-cache" })
    .then(function (resp) {
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      return resp.json();
    })
    .then(function (payload) {
      var rows = payload.rows || [];
      document.getElementById("lastUpdated").textContent =
        "Last refreshed: " + formatTimestamp(payload.generatedAt);
      document.getElementById("rowCount").textContent = rows.length + " packages tracked";

      var counts = { Released: 0, "To Release": 0, "In Progress": 0, "Not Started": 0 };
      var inProgressByKind = { refresh: 0, "self-serve": 0 };
      rows.forEach(function (r) {
        if (counts[r.releaseStatus] !== undefined) counts[r.releaseStatus] += 1;
        if (r.releaseStatus === "In Progress" && inProgressByKind[r.releaseBy] !== undefined) {
          inProgressByKind[r.releaseBy] += 1;
        }
      });
      var total = rows.length;
      var released = counts.Released;
      var pct = total ? Math.round((released / total) * 100) : 0;
      document.getElementById("stat-released").textContent = released;
      document.getElementById("stat-total").textContent = total;
      document.getElementById("stat-percent").textContent = "(" + pct + "%)";
      document.getElementById("stat-bar").style.width = pct + "%";
      document.getElementById("stat-to-release").textContent = counts["To Release"];
      document.getElementById("stat-in-progress").textContent = counts["In Progress"];
      document.getElementById("stat-in-progress-breakdown").textContent =
        inProgressByKind.refresh + " refresh · " + inProgressByKind["self-serve"] + " self-serve";
      document.getElementById("stat-not-started").textContent = counts["Not Started"];
      document.getElementById("stats").hidden = false;

      renderPlanner(payload.planner);

      populateFilter(document.getElementById("filter-status"), rows.map(function (r) { return r.releaseStatus; }));
      populateFilter(document.getElementById("filter-by"), rows.map(function (r) { return r.releaseBy; }));

      var table = new DataTable("#grid", {
        data: rows,
        deferRender: true,
        pageLength: 50,
        order: [[6, "asc"], [0, "asc"]],
        columns: [
          { data: "service", title: "Service", render: plain },
          { data: "armNamespace", title: "ARM Namespace", render: plain },
          { data: "specFolder", title: "Spec Folder", render: plain },
          { data: "sdkPackageName", title: "SDK Package Name", render: plain },
          { data: "specsApiVersion", title: "Specs API Version", render: plain },
          {
            data: "sdkPr",
            title: "SDK PR",
            render: function (data, type) {
              if (type === "display") return prLink(data);
              return data ? data.number : "";
            },
          },
          {
            data: "releaseStatus",
            title: "Release Status",
            render: function (data, type) {
              if (type === "display") return statusBadge(data);
              return data || "";
            },
          },
          {
            data: "releaseBy",
            title: "Release By",
            render: function (data, type) {
              if (type === "display") return releaseByBadge(data);
              return data || "";
            },
          },
        ],
      });

      document.getElementById("filter-status").addEventListener("change", function (e) {
        var v = e.target.value;
        table.column(6).search(v ? "^" + v + "$" : "", true, false).draw();
      });
      document.getElementById("filter-by").addEventListener("change", function (e) {
        var v = e.target.value;
        table.column(7).search(v ? "^" + v + "$" : "", true, false).draw();
      });
    })
    .catch(function (err) {
      document.getElementById("lastUpdated").textContent = "Failed to load data: " + err.message;
    });
})();
