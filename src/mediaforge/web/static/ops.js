/* Operations UI: groups, rules, language profiles, workers, snapshots,
 * maintenance windows, diagnostics and the audit log.
 *
 * Backs the three admin-only Settings tabs added alongside this file
 * (Rules & Languages / Operations / Audit Log) plus the Groups section inside
 * the Authentication tab.
 *
 * Conventions this file follows, none of them optional here:
 *  - window.mfEscape is the ONLY escaper (see mf_escape.js). Every value that
 *    reaches innerHTML goes through it, including ones that look safe: group
 *    names, rule names, worker error strings and audit targets are all user or
 *    remote controlled.
 *  - No inline onclick with interpolated data. Handlers are attached by
 *    delegation on a container and read data-* attributes, so a value
 *    containing a quote cannot become code.
 *  - Panels load lazily, on first switchTab() into them. The audit log can be
 *    large and the worker list polls; neither should cost anything on a page
 *    load that never opens those tabs.
 */
(function () {
  "use strict";

  var esc = window.mfEscape || function (s) { return String(s == null ? "" : s); };

  function toast(msg, type) {
    if (typeof window.showToast === "function") { window.showToast(msg, type); }
  }

  function T(key, fallback) {
    var dict = window.OPS_I18N || {};
    return dict[key] || fallback || key;
  }

  function api(path, options) {
    options = options || {};
    if (options.body && typeof options.body !== "string") {
      options.body = JSON.stringify(options.body);
      options.headers = Object.assign({ "Content-Type": "application/json" },
                                      options.headers || {});
    }
    return fetch(path, options).then(function (resp) {
      return resp.json().catch(function () { return {}; }).then(function (data) {
        if (!resp.ok) {
          // Surface the server's reason rather than a generic failure: every
          // error this API returns is actionable ("builtin_readonly",
          // "duplicate_key", "verify_failed") and hiding it behind "something
          // went wrong" is what makes a settings page feel broken.
          var err = new Error(data.error || ("HTTP " + resp.status));
          err.data = data;
          throw err;
        }
        return data;
      });
    });
  }

  function fail(exc) { toast((exc && exc.message) || String(exc), "error"); }

  /* ---------------------------------------------------------------
   * Minimal modal. Deliberately local rather than reusing
   * MFDetailModal: that one is built around media detail (poster,
   * hero, episode list) and every form here would be fighting it.
   * --------------------------------------------------------------- */
  var modalEl = null;

  function closeModal() {
    if (modalEl) { modalEl.remove(); modalEl = null; }
    if (window.MFScrollLock) { window.MFScrollLock.release("ops-modal"); }
    document.removeEventListener("keydown", onModalKey);
  }

  function onModalKey(ev) { if (ev.key === "Escape") { closeModal(); } }

  function openModal(title, bodyHtml, onSave) {
    closeModal();
    modalEl = document.createElement("div");
    modalEl.className = "ops-modal-backdrop";
    modalEl.innerHTML =
      '<div class="ops-modal" role="dialog" aria-modal="true">' +
        '<div class="ops-modal-head">' +
          '<h3>' + esc(title) + '</h3>' +
          '<button type="button" class="ops-modal-x" aria-label="' + esc(T("close", "Close")) + '">&times;</button>' +
        '</div>' +
        '<div class="ops-modal-body">' + bodyHtml + '</div>' +
        '<div class="ops-modal-foot">' +
          '<button type="button" class="btn btn-ghost" data-act="cancel">' + esc(T("cancel", "Cancel")) + '</button>' +
          (onSave ? '<button type="button" class="btn btn-primary" data-act="save">' + esc(T("save", "Save")) + '</button>' : '') +
        '</div>' +
      '</div>';
    document.body.appendChild(modalEl);
    if (window.MFScrollLock) { window.MFScrollLock.acquire("ops-modal"); }
    document.addEventListener("keydown", onModalKey);

    modalEl.addEventListener("click", function (ev) {
      if (ev.target === modalEl || ev.target.closest(".ops-modal-x") ||
          ev.target.closest('[data-act="cancel"]')) {
        closeModal();
        return;
      }
      if (ev.target.closest('[data-act="save"]') && onSave) {
        Promise.resolve(onSave(modalEl)).then(function (ok) {
          if (ok !== false) { closeModal(); }
        }).catch(fail);
      }
    });
    var first = modalEl.querySelector("input, select, textarea");
    if (first) { first.focus(); }
    return modalEl;
  }

  function val(root, id) {
    var el = root.querySelector("#" + id);
    if (!el) { return ""; }
    return el.type === "checkbox" ? el.checked : el.value;
  }

  /* ===============================================================
   * Groups
   * =============================================================== */
  var permissionCatalogue = {};
  var libraryLocations = [];

  function loadGroups() {
    var box = document.getElementById("opsGroupList");
    if (!box) { return; }
    // Locations first, so the editor can offer the real ids rather than
    // asking the admin to remember them. A scope naming a location that does
    // not exist looks configured and restricts nothing.
    api("/api/ops/library-locations")
      .then(function (data) { libraryLocations = data.locations || []; })
      .catch(function () { libraryLocations = []; });
    api("/api/ops/groups").then(function (data) {
      permissionCatalogue = data.permissions || {};
      var groups = data.groups || [];
      if (!groups.length) {
        box.innerHTML = '<div class="settings-hint">' + esc(T("no_groups", "No groups.")) + '</div>';
        return;
      }
      box.innerHTML = groups.map(function (g) {
        var scoped = g.scope.indexOf("*") === -1;
        return '' +
          '<div class="ops-card' + (g.builtin ? " is-builtin" : "") + '">' +
            '<div class="ops-card-head">' +
              '<strong>' + esc(g.name) + '</strong>' +
              (g.builtin ? '<span class="ops-pill">' + esc(T("builtin", "Built-in")) + '</span>' : '') +
            '</div>' +
            '<div class="ops-card-meta">' +
              '<span>' + esc(g.members) + ' ' + esc(T("members", "members")) + '</span>' +
              '<span>' + (g.permissions.indexOf("*") !== -1
                  ? esc(T("all_permissions", "all permissions"))
                  : g.permissions.length + " " + esc(T("permissions", "permissions"))) + '</span>' +
              '<span>' + (scoped
                  ? esc(g.scope.join(", "))
                  : esc(T("all_libraries", "all libraries"))) + '</span>' +
            '</div>' +
            (g.description ? '<p class="ops-card-desc">' + esc(g.description) + '</p>' : '') +
            (g.builtin ? '' :
              '<div class="ops-card-actions">' +
                '<button type="button" class="btn btn-ghost" data-ops-group-edit="' + esc(g.id) + '">' + esc(T("edit", "Edit")) + '</button>' +
                '<button type="button" class="btn btn-ghost ops-danger" data-ops-group-del="' + esc(g.id) + '">' + esc(T("delete", "Delete")) + '</button>' +
              '</div>') +
          '</div>';
      }).join("");
      box._groups = groups;
    }).catch(function (exc) {
      box.innerHTML = '<div class="settings-hint">' + esc(exc.message) + '</div>';
    });
  }

  function groupForm(group) {
    var perms = (group && group.permissions) || [];
    var rows = Object.keys(permissionCatalogue).sort().map(function (key) {
      var checked = perms.indexOf(key) !== -1 ? " checked" : "";
      return '<label class="settings-checkbox-row ops-perm-row">' +
        '<input type="checkbox" class="chb-main" data-perm="' + esc(key) + '"' + checked + '>' +
        '<span><code>' + esc(key) + '</code></span></label>';
    }).join("");

    var scope = (group && group.scope) || ["*"];
    var scopeIsAll = scope.indexOf("*") !== -1;
    var locationRows = libraryLocations.map(function (loc) {
      var on = !scopeIsAll && scope.indexOf(loc.id) !== -1 ? " checked" : "";
      return '<label class="settings-checkbox-row ops-perm-row">' +
        '<input type="checkbox" class="chb-main" data-scope="' + esc(loc.id) + '"' + on + '>' +
        '<span>' + esc(loc.name) + '</span></label>';
    }).join("") || ('<div class="settings-hint">' +
        esc(T("no_locations", "No library locations configured.")) + '</div>');

    return '' +
      '<div class="settings-field"><label class="settings-field-label" for="opsGroupName">' +
        esc(T("name", "Name")) + '</label>' +
        '<input type="text" id="opsGroupName" value="' + esc((group && group.name) || "") + '"></div>' +
      '<div class="settings-field"><label class="settings-field-label" for="opsGroupKey">' +
        esc(T("key", "Key")) + '</label>' +
        '<input type="text" id="opsGroupKey" value="' + esc((group && group.key) || "") + '"' +
        (group ? " disabled" : "") + '></div>' +
      '<div class="settings-field"><label class="settings-field-label" for="opsGroupDesc">' +
        esc(T("description", "Description")) + '</label>' +
        '<input type="text" id="opsGroupDesc" value="' + esc((group && group.description) || "") + '"></div>' +
      '<div class="settings-field"><span class="settings-field-label">' +
        esc(T("library_scope", "Library scope")) + '</span>' +
        '<label class="settings-checkbox-row">' +
          '<input type="checkbox" class="chb-main" id="opsGroupScopeAll"' +
          (scopeIsAll ? " checked" : "") + '>' +
          '<span>' + esc(T("all_libraries", "all libraries")) + '</span></label>' +
        '<div class="ops-perm-grid" id="opsGroupScopeList"' +
          (scopeIsAll ? ' hidden' : '') + '>' + locationRows + '</div>' +
        '<span class="settings-hint">' + esc(T("scope_hint",
          "Comma-separated library location ids, or * for all. Naming any location restricts members to those.")) +
        '</span></div>' +
      '<div class="settings-field"><span class="settings-field-label">' +
        esc(T("permissions", "Permissions")) + '</span>' +
        '<div class="ops-perm-grid">' + rows + '</div></div>';
  }

  window.opsGroupEdit = function (groupId) {
    var box = document.getElementById("opsGroupList");
    var group = null;
    if (groupId != null && box && box._groups) {
      group = box._groups.filter(function (g) { return String(g.id) === String(groupId); })[0] || null;
    }
    var root = openModal(group ? T("edit_group", "Edit group") : T("new_group", "New group"),
      groupForm(group), function (rootEl) {
        var perms = Array.prototype.slice
          .call(rootEl.querySelectorAll("[data-perm]"))
          .filter(function (el) { return el.checked; })
          .map(function (el) { return el.getAttribute("data-perm"); });
        var scope = ["*"];
        if (!val(rootEl, "opsGroupScopeAll")) {
          scope = Array.prototype.slice
            .call(rootEl.querySelectorAll("[data-scope]"))
            .filter(function (el) { return el.checked; })
            .map(function (el) { return el.getAttribute("data-scope"); });
          // Unchecking everything means "no restriction", not "no access" --
          // a group that hides the whole library from its members is never
          // what somebody meant to build by clicking checkboxes off.
          if (!scope.length) { scope = ["*"]; }
        }
        var payload = {
          name: val(rootEl, "opsGroupName"),
          description: val(rootEl, "opsGroupDesc"),
          permissions: perms,
          scope: scope
        };
        var req = group
          ? api("/api/ops/groups/" + group.id, { method: "PUT", body: payload })
          : api("/api/ops/groups", {
              method: "POST",
              body: Object.assign({ key: val(rootEl, "opsGroupKey") }, payload)
            });
        return req.then(function () { loadGroups(); toast(T("saved", "Saved"), "success"); });
      });

    // "All libraries" hides the per-location list rather than disabling it, so
    // the previous selection is still there if the box is unchecked again.
    var allBox = root.querySelector("#opsGroupScopeAll");
    var list = root.querySelector("#opsGroupScopeList");
    if (allBox && list) {
      allBox.addEventListener("change", function () { list.hidden = allBox.checked; });
    }
  };

  document.addEventListener("click", function (ev) {
    var edit = ev.target.closest("[data-ops-group-edit]");
    if (edit) { window.opsGroupEdit(edit.getAttribute("data-ops-group-edit")); return; }
    var del = ev.target.closest("[data-ops-group-del]");
    if (del) {
      if (!confirm(T("confirm_delete_group", "Delete this group? Members keep their role but lose what this group granted."))) { return; }
      api("/api/ops/groups/" + del.getAttribute("data-ops-group-del"), { method: "DELETE" })
        .then(function () { loadGroups(); toast(T("deleted", "Deleted"), "success"); })
        .catch(fail);
    }
  });

  /* ===============================================================
   * Rules
   * =============================================================== */
  var ruleMeta = { fields: {}, operators: {}, actions: {} };

  function loadRules() {
    var box = document.getElementById("opsRuleList");
    if (!box) { return; }
    api("/api/ops/rules").then(function (data) {
      ruleMeta = { fields: data.fields || {}, operators: data.operators || {},
                   actions: data.actions || {} };
      var rules = data.rules || [];
      box._rules = rules;
      if (!rules.length) {
        box.innerHTML = '<div class="settings-hint">' + esc(T("no_rules", "No rules yet.")) + '</div>';
        return;
      }
      box.innerHTML = rules.map(function (r) {
        var conds = r.conditions.length
          ? r.conditions.map(function (c) {
              return esc(c.field) + " " + esc(c.op) + " " + esc(c.value);
            }).join(" &amp; ")
          : esc(T("always", "always"));
        var acts = Object.keys(r.actions).map(function (k) {
          return esc(k) + "=" + esc(String(r.actions[k]));
        }).join(", ") || esc(T("no_actions", "no actions"));
        return '' +
          '<div class="ops-card' + (r.enabled ? "" : " is-off") + '">' +
            '<div class="ops-card-head">' +
              '<strong>' + esc(r.name) + '</strong>' +
              '<span class="ops-pill">#' + esc(r.priority) + '</span>' +
              (r.stop ? '<span class="ops-pill">' + esc(T("stop", "stop")) + '</span>' : '') +
              (r.enabled ? '' : '<span class="ops-pill">' + esc(T("disabled", "disabled")) + '</span>') +
            '</div>' +
            '<div class="ops-card-rule"><code>' + conds + '</code> &rarr; <code>' + acts + '</code></div>' +
            '<div class="ops-card-actions">' +
              '<button type="button" class="btn btn-ghost" data-ops-rule-edit="' + esc(r.id) + '">' + esc(T("edit", "Edit")) + '</button>' +
              '<button type="button" class="btn btn-ghost ops-danger" data-ops-rule-del="' + esc(r.id) + '">' + esc(T("delete", "Delete")) + '</button>' +
            '</div>' +
          '</div>';
      }).join("");
    }).catch(function (exc) {
      box.innerHTML = '<div class="settings-hint">' + esc(exc.message) + '</div>';
    });
  }

  function optionList(obj, selected) {
    return Object.keys(obj).map(function (k) {
      return '<option value="' + esc(k) + '"' + (k === selected ? " selected" : "") + '>' + esc(k) + '</option>';
    }).join("");
  }

  function conditionRow(cond) {
    cond = cond || { field: "title", op: "contains", value: "" };
    return '<div class="ops-cond-row">' +
      '<select data-cond-field>' + optionList(ruleMeta.fields, cond.field) + '</select>' +
      '<select data-cond-op>' + optionList(ruleMeta.operators, cond.op) + '</select>' +
      '<input type="text" data-cond-value value="' + esc(cond.value) + '">' +
      '<button type="button" class="btn btn-ghost ops-danger" data-cond-remove>&times;</button>' +
      '</div>';
  }

  function ruleForm(rule) {
    var conds = ((rule && rule.conditions) || []).map(conditionRow).join("");
    var actions = Object.keys(ruleMeta.actions).map(function (key) {
      var current = rule && rule.actions && rule.actions[key];
      var isBool = typeof current === "boolean" ||
        ["encode_after", "upscale_after", "download_subtitles", "skip", "notify"].indexOf(key) !== -1;
      if (isBool) {
        return '<label class="settings-checkbox-row ops-perm-row">' +
          '<input type="checkbox" class="chb-main" data-action="' + esc(key) + '"' +
          (current ? " checked" : "") + '><span><code>' + esc(key) + '</code></span></label>';
      }
      return '<div class="settings-field ops-action-field">' +
        '<label class="settings-field-label"><code>' + esc(key) + '</code></label>' +
        '<input type="text" data-action-text="' + esc(key) + '" value="' +
        esc(current == null ? "" : String(current)) + '"></div>';
    }).join("");

    return '' +
      '<div class="ops-form-row">' +
        '<div class="settings-field"><label class="settings-field-label" for="opsRuleName">' +
          esc(T("name", "Name")) + '</label>' +
          '<input type="text" id="opsRuleName" value="' + esc((rule && rule.name) || "") + '"></div>' +
        '<div class="settings-field"><label class="settings-field-label" for="opsRulePriority">' +
          esc(T("priority", "Priority")) + '</label>' +
          '<input type="number" id="opsRulePriority" min="0" max="9999" value="' +
          esc(rule ? rule.priority : 100) + '"></div>' +
      '</div>' +
      '<div class="settings-row" style="gap:18px;flex-wrap:wrap;">' +
        '<label class="settings-checkbox-row"><input type="checkbox" class="chb-main" id="opsRuleEnabled"' +
          (!rule || rule.enabled ? " checked" : "") + '><span>' + esc(T("enabled", "Enabled")) + '</span></label>' +
        '<label class="settings-checkbox-row"><input type="checkbox" class="chb-main" id="opsRuleStop"' +
          (rule && rule.stop ? " checked" : "") + '><span>' + esc(T("stop_after_match", "Stop after this rule")) + '</span></label>' +
      '</div>' +
      '<div class="settings-field"><span class="settings-field-label">' + esc(T("conditions", "Conditions")) + '</span>' +
        '<div id="opsCondList">' + conds + '</div>' +
        '<button type="button" class="btn btn-ghost" id="opsCondAdd">' + esc(T("add_condition", "Add condition")) + '</button>' +
        '<span class="settings-hint">' + esc(T("cond_hint", "All conditions must match. No conditions means the rule always applies.")) + '</span>' +
      '</div>' +
      '<div class="settings-field"><span class="settings-field-label">' + esc(T("actions", "Actions")) + '</span>' +
        '<div class="ops-action-grid">' + actions + '</div>' +
        '<span class="settings-hint">' + esc(T("action_hint", "Leave a text action empty to not set it.")) + '</span>' +
      '</div>';
  }

  window.opsRuleEdit = function (ruleId) {
    var box = document.getElementById("opsRuleList");
    var rule = null;
    if (ruleId != null && box && box._rules) {
      rule = box._rules.filter(function (r) { return String(r.id) === String(ruleId); })[0] || null;
    }
    var root = openModal(rule ? T("edit_rule", "Edit rule") : T("new_rule", "New rule"),
      ruleForm(rule), function (rootEl) {
        var conditions = Array.prototype.slice
          .call(rootEl.querySelectorAll(".ops-cond-row"))
          .map(function (row) {
            return {
              field: row.querySelector("[data-cond-field]").value,
              op: row.querySelector("[data-cond-op]").value,
              value: row.querySelector("[data-cond-value]").value
            };
          });
        var actions = {};
        rootEl.querySelectorAll("[data-action]").forEach(function (el) {
          if (el.checked) { actions[el.getAttribute("data-action")] = true; }
        });
        rootEl.querySelectorAll("[data-action-text]").forEach(function (el) {
          if (el.value.trim() !== "") { actions[el.getAttribute("data-action-text")] = el.value.trim(); }
        });
        var payload = {
          name: val(rootEl, "opsRuleName"),
          priority: parseInt(val(rootEl, "opsRulePriority"), 10) || 100,
          enabled: val(rootEl, "opsRuleEnabled"),
          stop: val(rootEl, "opsRuleStop"),
          conditions: conditions,
          actions: actions
        };
        var req = rule
          ? api("/api/ops/rules/" + rule.id, { method: "PUT", body: payload })
          : api("/api/ops/rules", { method: "POST", body: payload });
        return req.then(function () { loadRules(); toast(T("saved", "Saved"), "success"); });
      });

    root.querySelector("#opsCondAdd").addEventListener("click", function () {
      root.querySelector("#opsCondList").insertAdjacentHTML("beforeend", conditionRow(null));
    });
    root.addEventListener("click", function (ev) {
      var rm = ev.target.closest("[data-cond-remove]");
      if (rm) { rm.closest(".ops-cond-row").remove(); }
    });
  };

  window.opsRuleTestOpen = function () {
    var fields = Object.keys(ruleMeta.fields).map(function (key) {
      return '<div class="settings-field"><label class="settings-field-label">' +
        '<code>' + esc(key) + '</code></label>' +
        '<input type="text" data-ctx="' + esc(key) + '"></div>';
    }).join("");
    var root = openModal(T("test_rules", "Test rules"),
      '<div class="settings-hint">' +
        esc(T("test_hint", "Describe a hypothetical download. The result shows which rules match, in order, and the actions that would apply.")) +
      '</div><div class="ops-form-grid">' + fields + '</div>' +
      '<button type="button" class="btn btn-primary" id="opsRuleRun" style="margin-top:12px;">' +
        esc(T("run", "Run")) + '</button>' +
      '<pre class="ops-result" id="opsRuleResult"></pre>', null);

    root.querySelector("#opsRuleRun").addEventListener("click", function () {
      var context = {};
      root.querySelectorAll("[data-ctx]").forEach(function (el) {
        if (el.value.trim() !== "") { context[el.getAttribute("data-ctx")] = el.value.trim(); }
      });
      api("/api/ops/rules/test", { method: "POST", body: { context: context } })
        .then(function (data) {
          root.querySelector("#opsRuleResult").textContent =
            JSON.stringify(data.result, null, 2);
        }).catch(fail);
    });
  };

  document.addEventListener("click", function (ev) {
    var edit = ev.target.closest("[data-ops-rule-edit]");
    if (edit) { window.opsRuleEdit(edit.getAttribute("data-ops-rule-edit")); return; }
    var del = ev.target.closest("[data-ops-rule-del]");
    if (del) {
      if (!confirm(T("confirm_delete", "Delete this entry?"))) { return; }
      api("/api/ops/rules/" + del.getAttribute("data-ops-rule-del"), { method: "DELETE" })
        .then(function () { loadRules(); toast(T("deleted", "Deleted"), "success"); })
        .catch(fail);
    }
  });

  /* ===============================================================
   * Language profiles
   * =============================================================== */
  function loadProfiles() {
    var box = document.getElementById("opsProfileList");
    if (!box) { return; }
    api("/api/ops/language-profiles").then(function (data) {
      var profiles = data.profiles || [];
      box._profiles = profiles;
      if (!profiles.length) {
        box.innerHTML = '<div class="settings-hint">' + esc(T("no_profiles", "No profiles yet.")) + '</div>';
        return;
      }
      box.innerHTML = profiles.map(function (p) {
        return '' +
          '<div class="ops-card">' +
            '<div class="ops-card-head"><strong>' + esc(p.name) + '</strong>' +
              '<span class="ops-pill">' + esc(p.grab_all ? T("all_languages", "all") : T("first_match", "first match")) + '</span>' +
            '</div>' +
            '<div class="ops-card-meta">' +
              '<span><code>' + esc(p.chain.join(" → ")) + '</code></span>' +
              '<span>' + esc(p.titles) + ' ' + esc(T("titles", "titles")) + '</span>' +
            '</div>' +
            '<div class="ops-card-actions">' +
              '<button type="button" class="btn btn-ghost" data-ops-profile-edit="' + esc(p.id) + '">' + esc(T("edit", "Edit")) + '</button>' +
              '<button type="button" class="btn btn-ghost ops-danger" data-ops-profile-del="' + esc(p.id) + '">' + esc(T("delete", "Delete")) + '</button>' +
            '</div>' +
          '</div>';
      }).join("");
    }).catch(function (exc) {
      box.innerHTML = '<div class="settings-hint">' + esc(exc.message) + '</div>';
    });
  }

  window.opsProfileEdit = function (profileId) {
    var box = document.getElementById("opsProfileList");
    var profile = null;
    if (profileId != null && box && box._profiles) {
      profile = box._profiles.filter(function (p) { return String(p.id) === String(profileId); })[0] || null;
    }
    openModal(profile ? T("edit_profile", "Edit profile") : T("new_profile", "New profile"),
      '<div class="settings-field"><label class="settings-field-label" for="opsProfileName">' +
        esc(T("name", "Name")) + '</label>' +
        '<input type="text" id="opsProfileName" value="' + esc((profile && profile.name) || "") + '"></div>' +
      '<div class="settings-field"><label class="settings-field-label" for="opsProfileChain">' +
        esc(T("chain", "Language chain")) + '</label>' +
        '<input type="text" id="opsProfileChain" placeholder="de, en, ja" value="' +
        esc(((profile && profile.chain) || []).join(", ")) + '">' +
        '<span class="settings-hint">' + esc(T("chain_hint", "Comma-separated, in order of preference.")) + '</span></div>' +
      '<label class="settings-checkbox-row"><input type="checkbox" class="chb-main" id="opsProfileAll"' +
        (profile && profile.grab_all ? " checked" : "") + '>' +
        '<span>' + esc(T("grab_all", "Download every language in the chain")) + '</span></label>',
      function (root) {
        var payload = {
          name: val(root, "opsProfileName"),
          chain: String(val(root, "opsProfileChain")).split(",")
            .map(function (s) { return s.trim(); }).filter(Boolean),
          grab_all: val(root, "opsProfileAll")
        };
        var req = profile
          ? api("/api/ops/language-profiles/" + profile.id, { method: "PUT", body: payload })
          : api("/api/ops/language-profiles", { method: "POST", body: payload });
        return req.then(function () { loadProfiles(); toast(T("saved", "Saved"), "success"); });
      });
  };

  document.addEventListener("click", function (ev) {
    var edit = ev.target.closest("[data-ops-profile-edit]");
    if (edit) { window.opsProfileEdit(edit.getAttribute("data-ops-profile-edit")); return; }
    var del = ev.target.closest("[data-ops-profile-del]");
    if (del) {
      if (!confirm(T("confirm_delete", "Delete this entry?"))) { return; }
      api("/api/ops/language-profiles/" + del.getAttribute("data-ops-profile-del"), { method: "DELETE" })
        .then(function () { loadProfiles(); toast(T("deleted", "Deleted"), "success"); })
        .catch(fail);
    }
  });

  /* ===============================================================
   * Workers
   * =============================================================== */
  var workerTimer = null;

  function loadWorkers() {
    var box = document.getElementById("opsWorkerList");
    if (!box) { return; }
    api("/api/ops/workers").then(function (data) {
      var workers = data.workers || [];
      box.innerHTML = workers.map(function (w) {
        var state = w.stale ? "stale" : w.state;
        return '' +
          '<div class="ops-worker ops-state-' + esc(state) + '">' +
            '<div class="ops-worker-top">' +
              '<span class="ops-dot"></span>' +
              '<strong>' + esc(T(w.label, w.worker)) + '</strong>' +
              '<span class="ops-pill">' + esc(T("state_" + state, state)) + '</span>' +
            '</div>' +
            (w.detail ? '<div class="ops-worker-detail">' + esc(w.detail) + '</div>' : '') +
            '<dl class="ops-worker-facts">' +
              '<dt>' + esc(T("last_run", "Last run")) + '</dt><dd>' + esc(w.last_run || "—") + '</dd>' +
              '<dt>' + esc(T("next_run", "Next run")) + '</dt><dd>' + esc(w.next_run || "—") + '</dd>' +
              '<dt>' + esc(T("mode", "Mode")) + '</dt><dd>' + esc(w.mode || "—") + '</dd>' +
            '</dl>' +
            (w.last_error
              ? '<div class="ops-worker-error">' + esc(w.last_error) + '</div>'
              : '') +
          '</div>';
      }).join("");
    }).catch(function (exc) {
      box.innerHTML = '<div class="settings-hint">' + esc(exc.message) + '</div>';
    });
  }

  function startWorkerPolling() {
    stopWorkerPolling();
    loadWorkers();
    workerTimer = setInterval(function () {
      // Stop polling while the tab is hidden. A settings page left open in a
      // background tab used to keep hitting the server every 10 s forever.
      if (document.hidden) { return; }
      var panel = document.getElementById("tab-operations");
      if (!panel || !panel.classList.contains("active")) { stopWorkerPolling(); return; }
      loadWorkers();
    }, 10000);
  }

  function stopWorkerPolling() {
    if (workerTimer) { clearInterval(workerTimer); workerTimer = null; }
  }

  /* ===============================================================
   * Schema + snapshots
   * =============================================================== */
  function formatBytes(n) {
    n = Number(n) || 0;
    var units = ["B", "KB", "MB", "GB"], i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return n.toFixed(i ? 1 : 0) + " " + units[i];
  }

  function loadSchema() {
    var info = document.getElementById("opsSchemaInfo");
    var list = document.getElementById("opsSnapshotList");
    if (!info || !list) { return; }
    api("/api/ops/schema").then(function (data) {
      var mig = data.migrations || {};
      info.innerHTML =
        esc(T("schema_version", "Schema version")) + ": <strong>" + esc(mig.current) +
        "</strong> / " + esc(mig.latest) +
        (mig.pending && mig.pending.length
          ? ' — <span class="ops-warn">' + esc(T("pending", "pending")) + ": " + esc(mig.pending.join(", ")) + "</span>"
          : "");

      var snaps = data.snapshots || [];
      if (!snaps.length) {
        list.innerHTML = '<div class="settings-hint">' + esc(T("no_snapshots", "No snapshots yet.")) + '</div>';
        return;
      }
      list.innerHTML = snaps.map(function (s) {
        return '' +
          '<div class="ops-card">' +
            '<div class="ops-card-head"><strong>' + esc(s.created_at) + '</strong>' +
              '<span class="ops-pill">' + esc(s.reason) + '</span></div>' +
            '<div class="ops-card-meta">' +
              '<span>' + esc(formatBytes(s.size)) + '</span>' +
              '<span>v' + esc(s.app_version || "?") + '</span>' +
              (s.note ? '<span>' + esc(s.note) + '</span>' : '') +
            '</div>' +
            '<div class="ops-card-actions">' +
              '<button type="button" class="btn btn-ghost" data-ops-snap-verify="' + esc(s.id) + '">' + esc(T("verify", "Verify")) + '</button>' +
              '<button type="button" class="btn btn-ghost" data-ops-snap-restore="' + esc(s.id) + '">' + esc(T("restore", "Restore")) + '</button>' +
              '<button type="button" class="btn btn-ghost ops-danger" data-ops-snap-del="' + esc(s.id) + '">' + esc(T("delete", "Delete")) + '</button>' +
            '</div>' +
            '<div class="ops-snap-result" data-snap-result="' + esc(s.id) + '"></div>' +
          '</div>';
      }).join("");
    }).catch(function (exc) {
      info.innerHTML = esc(exc.message);
    });
  }

  window.opsSnapshotCreate = function () {
    api("/api/ops/snapshots", { method: "POST", body: { note: "" } })
      .then(function () { loadSchema(); toast(T("snapshot_created", "Snapshot created"), "success"); })
      .catch(fail);
  };

  document.addEventListener("click", function (ev) {
    var verify = ev.target.closest("[data-ops-snap-verify]");
    if (verify) {
      var vid = verify.getAttribute("data-ops-snap-verify");
      var out = document.querySelector('[data-snap-result="' + CSS.escape(vid) + '"]');
      if (out) { out.textContent = T("checking", "Checking…"); }
      fetch("/api/ops/snapshots/" + encodeURIComponent(vid) + "/verify")
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (!out) { return; }
          var rows = Object.keys(data.tables || {}).map(function (k) {
            return k + ": " + data.tables[k];
          }).join(", ");
          out.className = "ops-snap-result " + (data.ok ? "is-ok" : "is-bad");
          out.textContent = (data.ok ? "✓ " : "✗ ") +
            T("integrity", "integrity") + ": " + (data.integrity || data.error || "?") +
            (rows ? " — " + rows : "");
        }).catch(fail);
      return;
    }
    var restore = ev.target.closest("[data-ops-snap-restore]");
    if (restore) {
      if (!confirm(T("confirm_restore",
        "Replace the live database with this snapshot? A snapshot of the current state is taken first. The server must be restarted afterwards."))) { return; }
      api("/api/ops/snapshots/" + encodeURIComponent(restore.getAttribute("data-ops-snap-restore")) + "/restore",
          { method: "POST", body: {} })
        .then(function () {
          toast(T("restored", "Restored — please restart the server now."), "success");
          loadSchema();
        }).catch(fail);
      return;
    }
    var del = ev.target.closest("[data-ops-snap-del]");
    if (del) {
      if (!confirm(T("confirm_delete", "Delete this entry?"))) { return; }
      api("/api/ops/snapshots/" + encodeURIComponent(del.getAttribute("data-ops-snap-del")),
          { method: "DELETE" })
        .then(function () { loadSchema(); toast(T("deleted", "Deleted"), "success"); })
        .catch(fail);
    }
  });

  /* ===============================================================
   * Maintenance windows
   * =============================================================== */
  var DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];

  function minutesToTime(m) {
    m = Math.max(0, Math.min(Number(m) || 0, 1440));
    var h = Math.floor(m / 60), mm = m % 60;
    return (h < 10 ? "0" : "") + h + ":" + (mm < 10 ? "0" : "") + mm;
  }

  function timeToMinutes(text) {
    var parts = String(text || "").split(":");
    return (parseInt(parts[0], 10) || 0) * 60 + (parseInt(parts[1], 10) || 0);
  }

  function loadWindows() {
    var box = document.getElementById("opsWindowList");
    var cur = document.getElementById("opsWindowCurrent");
    if (!box) { return; }
    api("/api/ops/maintenance").then(function (data) {
      var windows = data.windows || [];
      box._windows = windows;
      if (cur) {
        var c = data.current || {};
        cur.innerHTML = c.active
          ? '<span class="ops-warn">' + esc(T("window_active", "Active now")) + ": " +
            esc((c.windows || []).join(", ")) + " — " +
            esc(T("max_downloads", "max downloads")) + ": " + esc(c.max_downloads) + "</span>"
          : esc(T("no_window_active", "No window active — normal settings apply."));
      }
      if (!windows.length) {
        box.innerHTML = '<div class="settings-hint">' + esc(T("no_windows", "No maintenance windows.")) + '</div>';
        return;
      }
      box.innerHTML = windows.map(function (w) {
        var days = DAY_KEYS.filter(function (_d, i) { return (w.days_mask >> i) & 1; })
          .map(function (d) { return T("day_" + d, d); }).join(", ");
        return '' +
          '<div class="ops-card' + (w.enabled ? "" : " is-off") + '">' +
            '<div class="ops-card-head"><strong>' + esc(w.name) + '</strong>' +
              '<span class="ops-pill">' + esc(minutesToTime(w.start_minute)) + '–' +
                esc(minutesToTime(w.end_minute)) + '</span></div>' +
            '<div class="ops-card-meta">' +
              '<span>' + esc(days) + '</span>' +
              '<span>' + esc(T("max_downloads", "max downloads")) + ": " + esc(w.max_downloads) + '</span>' +
              '<span>' + esc(T("encoding", "encoding")) + ": " + esc(w.allow_encoding ? "✓" : "✗") + '</span>' +
              '<span>' + esc(T("upscaling", "upscaling")) + ": " + esc(w.allow_upscale ? "✓" : "✗") + '</span>' +
              '<span>' + esc(T("scanning", "scanning")) + ": " + esc(w.allow_scan ? "✓" : "✗") + '</span>' +
            '</div>' +
            '<div class="ops-card-actions">' +
              '<button type="button" class="btn btn-ghost" data-ops-win-edit="' + esc(w.id) + '">' + esc(T("edit", "Edit")) + '</button>' +
              '<button type="button" class="btn btn-ghost ops-danger" data-ops-win-del="' + esc(w.id) + '">' + esc(T("delete", "Delete")) + '</button>' +
            '</div>' +
          '</div>';
      }).join("");
    }).catch(function (exc) {
      box.innerHTML = '<div class="settings-hint">' + esc(exc.message) + '</div>';
    });
  }

  window.opsWindowEdit = function (windowId) {
    var box = document.getElementById("opsWindowList");
    var win = null;
    if (windowId != null && box && box._windows) {
      win = box._windows.filter(function (w) { return String(w.id) === String(windowId); })[0] || null;
    }
    var mask = win ? win.days_mask : 127;
    var dayBoxes = DAY_KEYS.map(function (d, i) {
      return '<label class="settings-checkbox-row ops-day">' +
        '<input type="checkbox" class="chb-main" data-day="' + i + '"' +
        ((mask >> i) & 1 ? " checked" : "") + '><span>' + esc(T("day_" + d, d)) + '</span></label>';
    }).join("");

    openModal(win ? T("edit_window", "Edit window") : T("new_window", "New window"),
      '<div class="settings-field"><label class="settings-field-label" for="opsWinName">' +
        esc(T("name", "Name")) + '</label>' +
        '<input type="text" id="opsWinName" value="' + esc((win && win.name) || "") + '"></div>' +
      '<div class="ops-form-row">' +
        '<div class="settings-field"><label class="settings-field-label" for="opsWinStart">' +
          esc(T("from", "From")) + '</label><input type="time" id="opsWinStart" value="' +
          esc(minutesToTime(win ? win.start_minute : 480)) + '"></div>' +
        '<div class="settings-field"><label class="settings-field-label" for="opsWinEnd">' +
          esc(T("to", "To")) + '</label><input type="time" id="opsWinEnd" value="' +
          esc(minutesToTime(win ? win.end_minute : 1080)) + '"></div>' +
        '<div class="settings-field"><label class="settings-field-label" for="opsWinMax">' +
          esc(T("max_downloads", "Max downloads")) + '</label>' +
          '<input type="number" id="opsWinMax" min="0" max="32" value="' +
          esc(win ? win.max_downloads : 1) + '"></div>' +
      '</div>' +
      '<span class="settings-hint">' + esc(T("wrap_hint", "An end time before the start time wraps over midnight.")) + '</span>' +
      '<div class="settings-field"><span class="settings-field-label">' + esc(T("days", "Days")) + '</span>' +
        '<div class="ops-day-grid">' + dayBoxes + '</div></div>' +
      '<div class="settings-field"><span class="settings-field-label">' + esc(T("allowed_during_window", "Allowed during this window")) + '</span>' +
        '<label class="settings-checkbox-row"><input type="checkbox" class="chb-main" id="opsWinEnc"' +
          (win && win.allow_encoding ? " checked" : "") + '><span>' + esc(T("encoding", "Encoding")) + '</span></label>' +
        '<label class="settings-checkbox-row"><input type="checkbox" class="chb-main" id="opsWinUps"' +
          (win && win.allow_upscale ? " checked" : "") + '><span>' + esc(T("upscaling", "Upscaling")) + '</span></label>' +
        '<label class="settings-checkbox-row"><input type="checkbox" class="chb-main" id="opsWinScan"' +
          (!win || win.allow_scan ? " checked" : "") + '><span>' + esc(T("scanning", "Library scans")) + '</span></label>' +
      '</div>' +
      '<label class="settings-checkbox-row"><input type="checkbox" class="chb-main" id="opsWinEnabled"' +
        (!win || win.enabled ? " checked" : "") + '><span>' + esc(T("enabled", "Enabled")) + '</span></label>',
      function (root) {
        var days = 0;
        root.querySelectorAll("[data-day]").forEach(function (el) {
          if (el.checked) { days |= (1 << parseInt(el.getAttribute("data-day"), 10)); }
        });
        var payload = {
          name: val(root, "opsWinName"),
          enabled: val(root, "opsWinEnabled"),
          days_mask: days,
          start_minute: timeToMinutes(val(root, "opsWinStart")),
          end_minute: timeToMinutes(val(root, "opsWinEnd")),
          max_downloads: parseInt(val(root, "opsWinMax"), 10) || 0,
          allow_encoding: val(root, "opsWinEnc"),
          allow_upscale: val(root, "opsWinUps"),
          allow_scan: val(root, "opsWinScan")
        };
        var req = win
          ? api("/api/ops/maintenance/" + win.id, { method: "PUT", body: payload })
          : api("/api/ops/maintenance", { method: "POST", body: payload });
        return req.then(function () { loadWindows(); toast(T("saved", "Saved"), "success"); });
      });
  };

  document.addEventListener("click", function (ev) {
    var edit = ev.target.closest("[data-ops-win-edit]");
    if (edit) { window.opsWindowEdit(edit.getAttribute("data-ops-win-edit")); return; }
    var del = ev.target.closest("[data-ops-win-del]");
    if (del) {
      if (!confirm(T("confirm_delete", "Delete this entry?"))) { return; }
      api("/api/ops/maintenance/" + del.getAttribute("data-ops-win-del"), { method: "DELETE" })
        .then(function () { loadWindows(); toast(T("deleted", "Deleted"), "success"); })
        .catch(fail);
    }
  });

  /* ===============================================================
   * Scoped API keys
   * =============================================================== */
  var scopeCatalogue = {};

  function loadApiKeys() {
    var box = document.getElementById("opsApiKeyList");
    if (!box) { return; }
    api("/api/ops/api-keys").then(function (data) {
      scopeCatalogue = data.scopes || {};
      var keys = data.keys || [];
      if (!keys.length) {
        box.innerHTML = '<div class="settings-hint">' +
          esc(T("no_api_keys", "No scoped keys yet.")) + '</div>';
        return;
      }
      box.innerHTML = keys.map(function (k) {
        var state = !k.enabled ? T("disabled", "disabled")
          : k.expired ? T("expired", "expired")
          : T("active", "active");
        return '' +
          '<div class="ops-card' + (k.enabled && !k.expired ? "" : " is-off") + '">' +
            '<div class="ops-card-head"><strong>' + esc(k.name) + '</strong>' +
              '<span class="ops-pill">' + esc(state) + '</span></div>' +
            '<div class="ops-card-meta">' +
              '<span><code>' + esc(k.key_prefix) + '…</code></span>' +
              '<span>' + esc(k.scopes.length) + ' ' + esc(T("scopes", "scopes")) + '</span>' +
              '<span>' + esc(T("last_used", "last used")) + ': ' + esc(k.last_used || "—") + '</span>' +
              (k.expires_at ? '<span>' + esc(T("expires", "expires")) + ': ' + esc(k.expires_at) + '</span>' : '') +
            '</div>' +
            '<div class="ops-card-rule"><code>' + esc(k.scopes.join(", ")) + '</code></div>' +
            '<div class="ops-card-actions">' +
              '<button type="button" class="btn btn-ghost" data-ops-key-toggle="' + esc(k.id) + '" ' +
                'data-enabled="' + (k.enabled ? "1" : "0") + '">' +
                esc(k.enabled ? T("disable", "Disable") : T("enable", "Enable")) + '</button>' +
              '<button type="button" class="btn btn-ghost ops-danger" data-ops-key-del="' + esc(k.id) + '">' +
                esc(T("revoke", "Revoke")) + '</button>' +
            '</div>' +
          '</div>';
      }).join("");
    }).catch(function (exc) {
      box.innerHTML = '<div class="settings-hint">' + esc(exc.message) + '</div>';
    });
  }

  window.opsApiKeyCreate = function () {
    var rows = Object.keys(scopeCatalogue).sort().map(function (key) {
      return '<label class="settings-checkbox-row ops-perm-row">' +
        '<input type="checkbox" class="chb-main" data-scope-pick="' + esc(key) + '">' +
        '<span><code>' + esc(key) + '</code></span></label>';
    }).join("");

    openModal(T("new_api_key", "New API key"),
      '<div class="settings-field"><label class="settings-field-label" for="opsKeyName">' +
        esc(T("name", "Name")) + '</label>' +
        '<input type="text" id="opsKeyName" placeholder="Home Assistant"></div>' +
      '<div class="settings-field"><label class="settings-field-label" for="opsKeyExpires">' +
        esc(T("expires_optional", "Expires (optional)")) + '</label>' +
        '<input type="date" id="opsKeyExpires"></div>' +
      '<div class="settings-field"><span class="settings-field-label">' +
        esc(T("scopes", "Scopes")) + '</span>' +
        '<div class="ops-perm-grid">' + rows + '</div>' +
        '<span class="settings-hint">' + esc(T("scope_hint_key",
          "Pick only what the client needs. A dashboard that shows a queue count needs status:read and nothing else.")) +
        '</span></div>',
      function (root) {
        var scopes = Array.prototype.slice
          .call(root.querySelectorAll("[data-scope-pick]"))
          .filter(function (el) { return el.checked; })
          .map(function (el) { return el.getAttribute("data-scope-pick"); });
        if (!scopes.length) {
          toast(T("pick_a_scope", "Pick at least one scope."), "error");
          return false;
        }
        var expires = val(root, "opsKeyExpires");
        return api("/api/ops/api-keys", {
          method: "POST",
          body: {
            name: val(root, "opsKeyName"),
            scopes: scopes,
            // A date input gives a bare date; the server compares against a
            // full timestamp, so pin it to the end of that day rather than
            // to midnight — "expires on the 5th" should include the 5th.
            expires_at: expires ? (expires + "T23:59:59") : null
          }
        }).then(function (res) {
          loadApiKeys();
          showNewKey(res.key);
        });
      });
  };

  function showNewKey(plaintext) {
    // Only the hash is stored, so this is the one and only time the key
    // exists anywhere outside the client. The dialog says so, and there is
    // deliberately no "show key" anywhere else in the UI to contradict it.
    openModal(T("api_key_created", "API key created"),
      '<p class="settings-hint">' + esc(T("copy_now",
        "Copy it now. Only a hash is stored, so this key cannot be shown again — if you lose it, revoke it and create another.")) +
      '</p>' +
      '<div class="settings-field">' +
        '<input type="text" id="opsNewKeyValue" readonly value="' + esc(plaintext) + '" ' +
        'style="font-family:monospace;font-size:0.82rem;"></div>' +
      '<button type="button" class="btn btn-primary" id="opsCopyKey">' +
        esc(T("copy", "Copy")) + '</button>', null);

    var field = document.getElementById("opsNewKeyValue");
    if (field) { field.focus(); field.select(); }
    var copy = document.getElementById("opsCopyKey");
    if (copy) {
      copy.addEventListener("click", function () {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(plaintext).then(function () {
            toast(T("copied", "Copied"), "success");
          }).catch(function () { if (field) { field.select(); } });
        } else if (field) {
          // No clipboard API on plain http, which is a normal way to run this.
          field.select();
          document.execCommand("copy");
          toast(T("copied", "Copied"), "success");
        }
      });
    }
  }

  document.addEventListener("click", function (ev) {
    var toggle = ev.target.closest("[data-ops-key-toggle]");
    if (toggle) {
      api("/api/ops/api-keys/" + toggle.getAttribute("data-ops-key-toggle"),
          { method: "PUT", body: { enabled: toggle.getAttribute("data-enabled") !== "1" } })
        .then(loadApiKeys).catch(fail);
      return;
    }
    var del = ev.target.closest("[data-ops-key-del]");
    if (del) {
      if (!confirm(T("confirm_revoke",
        "Revoke this key? Anything using it stops working immediately."))) { return; }
      api("/api/ops/api-keys/" + del.getAttribute("data-ops-key-del"), { method: "DELETE" })
        .then(function () { loadApiKeys(); toast(T("deleted", "Deleted"), "success"); })
        .catch(fail);
    }
  });

  /* ===============================================================
   * Settings profiles
   * =============================================================== */
  window.opsProfileExport = function () {
    window.location.href = "/api/ops/profile/export";
  };

  window.opsProfilePreview = function (input) {
    var box = document.getElementById("opsProfilePreview");
    var file = input && input.files && input.files[0];
    if (!file || !box) { return; }
    // Cap the read. A profile is a few kilobytes of settings; anything larger
    // is the wrong file, and reading it into memory first is how a mistaken
    // drag-and-drop of a video freezes the tab.
    if (file.size > 512 * 1024) {
      box.innerHTML = '<div class="settings-hint">' + esc(T("file_too_large", "That file is too large to be a settings profile.")) + '</div>';
      input.value = "";
      return;
    }
    var reader = new FileReader();
    reader.onload = function () {
      api("/api/ops/profile/preview", { method: "POST", body: { file: reader.result } })
        .then(function (data) { renderProfilePreview(data, reader.result); })
        .catch(function (exc) {
          box.innerHTML = '<div class="settings-hint">' + esc(exc.message) + '</div>';
        });
    };
    reader.readAsText(file);
    input.value = "";
  };

  function renderProfilePreview(data, rawText) {
    var box = document.getElementById("opsProfilePreview");
    if (!box) { return; }
    if (!data.changes.length) {
      box.innerHTML = '<div class="settings-hint">' +
        esc(T("profile_no_changes", "This profile matches the current configuration — nothing to apply.")) +
        '</div>';
      return;
    }
    box.innerHTML =
      '<div class="settings-hint" style="margin-bottom:10px;">' +
        esc(data.name || "") + ' · ' + esc(data.app_version || "?") + ' · ' +
        esc(data.changes.length) + ' ' + esc(T("changes", "change(s)")) +
        (data.unchanged ? ' · ' + esc(data.unchanged) + ' ' + esc(T("unchanged", "unchanged")) : '') +
        (data.refused.length
          ? ' · <span class="ops-warn">' + esc(data.refused.length) + ' ' + esc(T("refused", "refused")) + '</span>'
          : '') +
      '</div>' +
      '<div class="ops-audit-list">' +
        data.changes.map(function (c) {
          return '<label class="ops-audit-row">' +
            '<input type="checkbox" class="chb-main" data-profile-key="' + esc(c.key) + '" checked>' +
            '<div class="ops-audit-main"><code>' + esc(c.key) + '</code></div>' +
            '<div class="ops-audit-who">' + esc(c.from == null ? "—" : c.from) +
              ' &rarr; <strong>' + esc(c.to) + '</strong></div>' +
          '</label>';
        }).join("") +
      '</div>' +
      '<div class="settings-row" style="margin-top:12px;">' +
        '<button type="button" class="btn btn-primary" id="opsProfileApply">' +
          esc(T("apply_selected", "Apply selected")) + '</button>' +
      '</div>';

    box.querySelector("#opsProfileApply").addEventListener("click", function () {
      var keys = Array.prototype.slice.call(box.querySelectorAll("[data-profile-key]"))
        .filter(function (el) { return el.checked; })
        .map(function (el) { return el.getAttribute("data-profile-key"); });
      if (!keys.length) { return; }
      api("/api/ops/profile/import", { method: "POST", body: { file: rawText, keys: keys } })
        .then(function (res) {
          toast(T("profile_applied", "Applied") + " (" + res.count + ")", "success");
          box.innerHTML = "";
        }).catch(fail);
    });
  }

  /* ===============================================================
   * Diagnostics
   * =============================================================== */
  window.opsDiagnostics = function () {
    // A plain navigation, not fetch+blob: the response is a file download and
    // the browser's own download handling is what the user expects.
    window.location.href = "/api/ops/diagnostics";
  };

  /* ===============================================================
   * Audit log
   * =============================================================== */
  var auditOffset = 0;
  var auditDebounce = null;
  var auditReady = false;

  window.opsAuditDebounced = function () {
    clearTimeout(auditDebounce);
    auditDebounce = setTimeout(function () { window.opsAuditLoad(0); }, 300);
  };

  function fillAuditFilters() {
    if (auditReady) { return Promise.resolve(); }
    return api("/api/ops/audit/stats").then(function (data) {
      var cat = document.getElementById("opsAuditCategory");
      var sev = document.getElementById("opsAuditSeverity");
      if (cat) {
        cat.innerHTML = '<option value="">' + esc(T("all", "All")) + "</option>" +
          (data.categories || []).map(function (c) {
            return '<option value="' + esc(c) + '">' + esc(T("cat_" + c, c)) + "</option>";
          }).join("");
      }
      if (sev) {
        sev.innerHTML = '<option value="">' + esc(T("all", "All")) + "</option>" +
          (data.severities || []).map(function (s) {
            return '<option value="' + esc(s) + '">' + esc(T("sev_" + s, s)) + "</option>";
          }).join("");
      }
      auditReady = true;
    });
  }

  function renderAuditStats(stats) {
    var box = document.getElementById("opsAuditStats");
    if (!box || !stats) { return; }
    box.innerHTML =
      '<div class="ops-stat"><b>' + esc(stats.total) + '</b><span>' + esc(T("entries", "entries")) + '</span></div>' +
      '<div class="ops-stat"><b>' + esc(stats.failures) + '</b><span>' + esc(T("failures", "failures")) + '</span></div>' +
      '<div class="ops-stat"><b>' + esc(formatBytes(stats.size)) + '</b><span>' + esc(T("size", "size")) + '</span></div>' +
      '<div class="ops-stat"><b>' + esc((stats.oldest || "—").slice(0, 10)) + '</b><span>' + esc(T("oldest", "oldest")) + '</span></div>';
  }

  window.opsAuditLoad = function (offset) {
    auditOffset = offset || 0;
    var list = document.getElementById("opsAuditList");
    if (!list) { return; }
    var params = new URLSearchParams({
      q: (document.getElementById("opsAuditSearch") || {}).value || "",
      category: (document.getElementById("opsAuditCategory") || {}).value || "",
      severity: (document.getElementById("opsAuditSeverity") || {}).value || "",
      limit: 50,
      offset: auditOffset
    });

    fillAuditFilters().then(function () {
      return api("/api/ops/audit/stats");
    }).then(function (data) {
      renderAuditStats(data.stats);
      return api("/api/ops/audit?" + params.toString());
    }).then(function (data) {
      var entries = data.entries || [];
      if (!entries.length) {
        list.innerHTML = '<div class="settings-hint">' + esc(T("no_entries", "No entries.")) + '</div>';
        renderPager(0, 0);
        return;
      }
      list.innerHTML = entries.map(function (e) {
        var detail = JSON.stringify(e.detail || {});
        return '' +
          '<div class="ops-audit-row ops-sev-' + esc(e.severity) + '">' +
            '<div class="ops-audit-when">' + esc(e.ts.replace("T", " ")) + '</div>' +
            '<div class="ops-audit-main">' +
              '<span class="ops-pill">' + esc(T("cat_" + e.category, e.category)) + '</span> ' +
              '<strong>' + esc(e.action) + '</strong>' +
              (e.target ? ' <span class="ops-audit-target">' + esc(e.target) + '</span>' : '') +
              (e.outcome !== "success" ? ' <span class="ops-warn">' + esc(e.outcome) + '</span>' : '') +
            '</div>' +
            '<div class="ops-audit-who">' + esc(e.actor_name || "—") +
              (e.ip ? ' <span class="ops-audit-ip">' + esc(e.ip) + '</span>' : '') + '</div>' +
            (detail !== "{}"
              ? '<button type="button" class="ops-audit-more" data-audit-detail>' + esc(T("details", "Details")) + '</button>' +
                '<pre class="ops-audit-detail" hidden>' + esc(detail) + '</pre>'
              : '') +
          '</div>';
      }).join("");
      renderPager(data.total, data.limit);
    }).catch(function (exc) {
      list.innerHTML = '<div class="settings-hint">' + esc(exc.message) + '</div>';
    });
  };

  function renderPager(total, limit) {
    var pager = document.getElementById("opsAuditPager");
    if (!pager) { return; }
    if (!total || total <= limit) { pager.innerHTML = ""; return; }
    var page = Math.floor(auditOffset / limit) + 1;
    var pages = Math.ceil(total / limit);
    pager.innerHTML =
      '<button type="button" class="btn btn-ghost" data-audit-page="' + Math.max(0, auditOffset - limit) + '"' +
        (auditOffset <= 0 ? " disabled" : "") + '>&larr;</button>' +
      '<span class="mf-pagination-info">' + esc(page) + " / " + esc(pages) + '</span>' +
      '<button type="button" class="btn btn-ghost" data-audit-page="' + (auditOffset + limit) + '"' +
        (page >= pages ? " disabled" : "") + '>&rarr;</button>';
  }

  document.addEventListener("click", function (ev) {
    var page = ev.target.closest("[data-audit-page]");
    if (page && !page.disabled) {
      window.opsAuditLoad(parseInt(page.getAttribute("data-audit-page"), 10) || 0);
      return;
    }
    var more = ev.target.closest("[data-audit-detail]");
    if (more) {
      var pre = more.nextElementSibling;
      if (pre) { pre.hidden = !pre.hidden; }
    }
  });

  window.opsAuditSaveRetention = function () {
    var field = document.getElementById("opsAuditRetention");
    if (!field) { return; }
    var days = Math.max(0, Math.min(parseInt(field.value, 10) || 0, 3650));
    // Reuses the ordinary settings endpoint rather than getting one of its
    // own: this is a setting, and a second write path to app_settings is a
    // second place for the encryption/validation rules to drift.
    api("/api/settings", { method: "PUT", body: { audit_retention_days: days } })
      .then(function () { toast(T("saved", "Saved"), "success"); })
      .catch(fail);
  };

  function loadAuditRetention() {
    var field = document.getElementById("opsAuditRetention");
    if (!field) { return; }
    api("/api/settings").then(function (data) {
      field.value = parseInt(data.audit_retention_days, 10) || 0;
    }).catch(function () { field.value = 0; });
  }

  window.opsAuditVerify = function () {
    api("/api/ops/audit/verify").then(function (data) {
      toast(data.ok
        ? T("chain_ok", "Chain intact") + " (" + data.checked + ")"
        : T("chain_broken", "Chain broken at entry ") + data.broken_at,
        data.ok ? "success" : "error");
    }).catch(fail);
  };

  window.opsAuditExport = function () {
    var params = new URLSearchParams({
      q: (document.getElementById("opsAuditSearch") || {}).value || "",
      category: (document.getElementById("opsAuditCategory") || {}).value || ""
    });
    window.location.href = "/api/ops/audit/export?" + params.toString();
  };

  /* ===============================================================
   * Lazy loading, driven by switchTab()
   * =============================================================== */
  var loaded = {};

  function activate(tab) {
    if (tab === "auth" && !loaded.auth) { loaded.auth = true; loadGroups(); }
    if (tab === "rules" && !loaded.rules) { loaded.rules = true; loadRules(); loadProfiles(); }
    if (tab === "api" && !loaded.api) { loaded.api = true; loadApiKeys(); }
    if (tab === "operations") {
      if (!loaded.operations) { loaded.operations = true; loadSchema(); loadWindows(); }
      startWorkerPolling();
    } else {
      stopWorkerPolling();
    }
    if (tab === "audit" && !loaded.audit) {
      loaded.audit = true;
      loadAuditRetention();
      window.opsAuditLoad(0);
    }
  }

  // switchTab() is settings.js's, and it is what every tab button calls. Wrap
  // it rather than duplicating its logic: the panels here have to load the
  // first time they are opened, and there is no event to hook otherwise.
  function hookSwitchTab() {
    var original = window.switchTab;
    if (typeof original !== "function" || original._opsWrapped) { return false; }
    window.switchTab = function (tab) {
      var result = original.apply(this, arguments);
      try { activate(tab); } catch (exc) { /* never break tab switching */ }
      return result;
    };
    window.switchTab._opsWrapped = true;
    return true;
  }

  document.addEventListener("DOMContentLoaded", function () {
    if (!hookSwitchTab()) {
      // settings.js may define switchTab after us depending on script order.
      var tries = 0;
      var timer = setInterval(function () {
        if (hookSwitchTab() || ++tries > 20) { clearInterval(timer); }
      }, 100);
    }
    // Deep link: /settings#operations should open loaded, not empty.
    var hash = (window.location.hash || "").replace("#", "");
    if (hash) { setTimeout(function () { activate(hash); }, 300); }
  });

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) { stopWorkerPolling(); }
  });
})();
