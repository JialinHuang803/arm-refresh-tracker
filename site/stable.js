(function () {
  "use strict";

  function plain(value) {
    return value == null ? "" : String(value);
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

  function isStableApi(specsApiVersion) {
    if (!specsApiVersion) return false;
    return specsApiVersion.indexOf("preview") === -1;
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

      // Filter: beta SDK version + stable API version + released status
      var candidates = rows.filter(function (r) {
        return isBeta(r.sdkVersion) && isStableApi(r.specsApiVersion) && r.releaseStatus === "Released";
      });

      document.getElementById("rowCount").textContent = candidates.length + " packages";
      document.getElementById("stat-candidates").textContent = candidates.length;
      document.getElementById("stats").hidden = false;

      new DataTable("#grid", {
        data: candidates,
        deferRender: true,
        pageLength: 50,
        order: [[0, "asc"]],
        columns: [
          { data: "service", title: "Service", render: plain },
          { data: "sdkPackageName", title: "SDK Package Name", render: plain },
          {
            data: "sdkVersion",
            title: "SDK Version",
            render: function (data, type) {
              if (type === "display" && isBeta(data)) {
                return '<span class="badge badge-beta">' + data + "</span>";
              }
              return data || "";
            },
          },
          {
            data: "specsApiVersion",
            title: "API Version",
            render: plain,
          },
          {
            data: "sdkPr",
            title: "SDK PR",
            render: function (data, type) {
              if (type === "display") return prLink(data);
              return data ? data.number : "";
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
    })
    .catch(function (err) {
      document.getElementById("lastUpdated").textContent = "Failed to load data: " + err.message;
    });
})();
