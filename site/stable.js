(function () {
  "use strict";

  function plain(value) {
    return value == null ? "" : String(value);
  }

  function statusBadge(value) {
    if (!value) return "";
    var className = "badge-" + value.replace(/\s+/g, "-");
    return '<span class="badge ' + className + '">' + value + "</span>";
  }

  function prLink(pr) {
    if (!pr) return "";
    return '<a href="' + pr.url + '" target="_blank" rel="noopener">#' + pr.number + "</a>";
  }

  function releaseByBadge(value) {
    if (!value) return "";
    return '<span class="badge tag-' + value + '">' + value + "</span>";
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

  function isBeta(version) {
    return version && version.indexOf("beta") !== -1;
  }

  function isStableApi(sdkApiVersions) {
    if (!sdkApiVersions || typeof sdkApiVersions !== "object") return false;
    var values = Object.values(sdkApiVersions);
    if (values.length === 0) return false;
    return values.every(function (v) {
      return String(v).indexOf("preview") === -1;
    });
  }

  function formatApiVersions(row) {
    // specsApiVersion is the field from data.json
    return row.specsApiVersion || "";
  }

  fetch("data.json", { cache: "no-cache" })
    .then(function (resp) {
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      return resp.json();
    })
    .then(function (payload) {
      var rows = payload.rows || [];
      document.getElementById("lastUpdated").textContent =
        "Last refreshed: " + formatTimestamp(payload.generatedAt);

      // Filter: beta SDK version + stable API versions (from metadata.json at merge) + released + releaseBy=refresh
      var candidates = rows.filter(function (r) {
        return isBeta(r.sdkVersion) && isStableApi(r.sdkApiVersions) && r.releaseStatus === "Released" && r.releaseBy === "refresh";
      });

      document.getElementById("rowCount").textContent = candidates.length + " packages";
      document.getElementById("stat-candidates").textContent = candidates.length;
      document.getElementById("stats").hidden = false;

      new DataTable("#grid", {
        data: candidates,
        deferRender: true,
        pageLength: 50,
        order: [[2, "asc"]],
        columns: [
          { data: "service", title: "Service", render: plain },
          { data: "sdkPackageName", title: "SDK Package Name", render: plain },
          {
            data: "releasedAt",
            title: "Last Release Date",
            render: function (data, type) {
              if (!data) return "";
              if (type === "sort" || type === "type") return data;
              return data.substring(0, 10);
            },
          },
          {
            data: "sdkVersion",
            title: "SDK Version (beta)",
            render: function (data, type) {
              if (type === "display" && isBeta(data)) {
                return '<span class="badge badge-beta">' + data + "</span>";
              }
              return data || "";
            },
          },
          {
            data: "sdkApiVersions",
            title: "API Version",
            render: function (data, type) {
              if (!data || typeof data !== "object") return "";
              return Object.values(data).join(", ");
            },
          },
          {
            data: "sdkPr",
            title: "SDK PR (beta)",
            className: "dt-body-left dt-head-left",
            render: function (data, type) {
              if (type === "display") return prLink(data);
              return data ? data.number : "";
            },
          },
          {
            data: "stableVersion",
            title: "SDK Version (stable)",
            render: function (data, type, row) {
              if (!data) return "";
              if (type === "display") {
                var cls = row.stableReleaseStatus === "Released" ? "badge-Released" : "badge-beta";
                return '<span class="badge ' + cls + '">' + data + "</span>";
              }
              return data;
            },
          },
          {
            data: "stablePr",
            title: "SDK PR (stable)",
            className: "dt-body-left dt-head-left",
            render: function (data, type) {
              if (type === "display") return prLink(data);
              return data ? data.number : "";
            },
          },
          {
            data: "stableReleaseStatus",
            title: "Stable Release Status",
            render: function (data, type) {
              if (type === "display") return statusBadge(data);
              return data || "";
            },
          },
        ],
      });
    })
    .catch(function (err) {
      document.getElementById("lastUpdated").textContent = "Failed to load data: " + err.message;
    });
})();
