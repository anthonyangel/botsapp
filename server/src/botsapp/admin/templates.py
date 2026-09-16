"""
HTML templates for the admin UI, styled with Material Design Lite (MDL)
loaded from cdnjs — rather than hand-rolling a design system for an
internal tool.

Still no template engine dependency: MDL supplies the look, f-strings +
html.escape still assemble the markup. Every value that originates from
WhatsApp (chat/group names, topics) is untrusted and passed through
html.escape before landing in markup.
"""

import html
import json
from typing import Any

_MDL_VERSION = "1.3.0"
_MDL_CSS = (
    f"https://cdnjs.cloudflare.com/ajax/libs/material-design-lite/{_MDL_VERSION}/"
    "material.indigo-pink.min.css"
)
_MDL_JS = (
    f"https://cdnjs.cloudflare.com/ajax/libs/material-design-lite/{_MDL_VERSION}/material.min.js"
)

_EXTRA_CSS = """
  /* MDL's indigo-pink theme is light-only; force it regardless of the
     browser/OS dark-mode preference rather than rendering half-themed. */
  html, body { background: #fafafa; color-scheme: light; }
  .mdl-layout__content { background: #fafafa; }
  .page-content { padding: 8px 24px 40px; max-width: 1000px; margin: 0 auto; }
  .avatar { width: 36px; height: 36px; border-radius: 50%; object-fit: cover;
            vertical-align: middle; margin-right: 10px; background: #e0e0e0; }
  .avatar-placeholder { display: inline-block; width: 36px; height: 36px; border-radius: 50%;
                         background: #bdbdbd; vertical-align: middle; margin-right: 10px; }
  .row-name { display: flex; align-items: center; max-width: 280px; }
  .row-name .meta { display: flex; flex-direction: column; min-width: 0; }
  .row-name code { font-size: 11px; color: #888; }
  /* A group's topic (WhatsApp lets these run to ~1000 chars, one long
     unbroken line in the worst case — e.g. a pasted email/URL) was
     blowing this column out to thousands of px wide with nothing to wrap
     on, shoving every other column off-screen. Truncate with an ellipsis
     instead; the full text is still in the title attribute on hover. */
  .row-name .meta > span, .row-name .meta > code, .row-name .meta > .row-topic {
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 100%;
  }
  .badge { display: inline-block; background: #ede7f6; color: #5e35b1; border-radius: 10px;
           padding: 2px 8px; font-size: 11px; margin-left: 6px; }
  /* MDL chips (https://getmdl.io/components/#chips-section) for tags —
     tinted to match the indigo .badge already used elsewhere in this UI,
     rather than MDL's default neutral grey. Pure CSS, no JS upgrade. */
  .tag-chips { display: flex; flex-wrap: wrap; gap: 4px; }
  .tag-chips:not(:empty) { margin-bottom: 6px; }
  .tag-chips .mdl-chip { background: #ede7f6; }
  .tag-chips .mdl-chip .mdl-chip__text { color: #5e35b1; font-size: 12px; font-weight: 500; }
  .tag-chips .mdl-chip__action { color: #5e35b1; opacity: 0.6; }
  .tag-chips .mdl-chip__action:hover { opacity: 1; }
  .tag-chips .mdl-chip__action .material-icons { font-size: 16px; }
  /* Material's "filter chip" pattern (m2.material.io/components/chips#filter-chips)
     layered on MDL's base chip — MDL ships one generic chip with no distinct
     variants, so the toggled/selected look (tinted fill + leading checkmark)
     is custom here, not stock MDL. */
  .filter-chip-bar { display: flex; flex-wrap: wrap; gap: 6px; margin: 10px 0; }
  .filter-chip-bar .mdl-chip--filter { cursor: pointer; user-select: none;
                                        background: #eeeeee; color: #616161; }
  .filter-chip-bar .mdl-chip--filter .filter-chip-check {
    display: none; font-size: 16px; vertical-align: middle; margin-right: 2px;
  }
  .filter-chip-bar .mdl-chip--filter .mdl-chip__text { font-size: 12px; font-weight: 500; }
  .filter-chip-bar .mdl-chip--filter.is-active { background: #5e35b1; color: #fff; }
  .filter-chip-bar .mdl-chip--filter.is-active .filter-chip-check { display: inline-block; }
  .status-dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%;
                margin-right: 8px; vertical-align: middle; }
  .status-dot.connected { background: #22c55e; }
  .status-dot.waiting { background: #ff9800; }
  .status-dot.disconnected { background: #ef5350; }
  .qr-card img { width: 240px; height: 240px; border-radius: 8px;
                 display: block; margin: 16px auto; }
  .meta-form { display: flex; gap: 8px; align-items: flex-end; flex-wrap: wrap; }
  .meta-form .mdl-textfield { width: 160px; margin-right: 0; }
  .empty-state { color: #757575; padding: 24px 0; text-align: center; }
  .tab-note { color: #757575; font-size: 13px; margin: 12px 0 4px; }
  .bridge-badge { background: rgba(255,255,255,0.2); color: #fff; border-radius: 10px;
                  padding: 2px 10px; font-size: 12px; font-weight: 500; vertical-align: middle;
                  margin-left: 8px; letter-spacing: 0.5px; }
  /* MDL's data-table assumes numeric content (right-aligned, tight padding)
     unless told otherwise per cell — every column here is names/forms, so
     without this every header and cell renders right-aligned and cramped. */
  .mdl-data-table td, .mdl-data-table th { text-align: left; }
  .table-scroll { overflow-x: auto; }
  .mdl-data-table { min-width: 640px; }
  .mdl-data-table .meta-form { flex-wrap: nowrap; }
  .mdl-data-table .meta-form .mdl-textfield { width: 130px; }
  .table-toolbar { margin: 12px 0 4px; max-width: 340px; }
  th[data-sort-key] { cursor: pointer; user-select: none; }
  th[data-sort-key]:hover { text-decoration: underline; }
  th[data-sort-key]::after { content: ""; font-size: 10px; }
  th.sort-asc::after { content: " \\25B2"; }
  th.sort-desc::after { content: " \\25BC"; }
"""


def _page(title: str, body: str, bridge_name: str = "") -> str:
    bridge_badge = (
        f'<span class="badge bridge-badge">{html.escape(bridge_name.upper())}</span>'
        if bridge_name
        else ""
    )
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <link rel="stylesheet" href="https://fonts.googleapis.com/css?family=Roboto:400,500,700">
  <link rel="stylesheet" href="https://fonts.googleapis.com/icon?family=Material+Icons">
  <link rel="stylesheet" href="{_MDL_CSS}">
  <style>{_EXTRA_CSS}</style>
</head>
<body>
  <div class="mdl-layout mdl-js-layout mdl-layout--fixed-header">
    <header class="mdl-layout__header">
      <div class="mdl-layout__header-row">
        <span class="mdl-layout-title">WhatsApp Admin {bridge_badge}</span>
        <div class="mdl-layout-spacer"></div>
        <nav class="mdl-navigation">
          <a class="mdl-navigation__link" href="/">Session</a>
          <a class="mdl-navigation__link" href="/chats">Chats &amp; Groups</a>
          <a class="mdl-navigation__link" href="/access">Access</a>
        </nav>
      </div>
    </header>
    <main class="mdl-layout__content">
      <div class="page-content">
        <h4>{html.escape(title)}</h4>
        {body}
      </div>
    </main>
  </div>
  <script src="{_MDL_JS}"></script>
</body>
</html>"""


def _avatar(url: str | None, alt: str) -> str:
    if url:
        src = html.escape(url, quote=True)
        alt_attr = html.escape(alt, quote=True)
        return f'<img class="avatar" src="{src}" alt="{alt_attr}">'
    return '<span class="avatar-placeholder"></span>'


def session_page(status: dict[str, Any], bridge_name: str = "") -> str:
    """``status``: SessionStatus as a dict (see bridge.py) — the shape
    admin.app._poll_status() builds via dataclasses.asdict(). Poll-only
    (see that function's docstring for why this matters): loading or
    refreshing this page never triggers a WhatsApp connect attempt on its
    own."""
    if status.get("errors"):
        body = f"""
        <div class="mdl-card mdl-shadow--2dp" style="width:100%;max-width:420px;padding:24px;">
          <p><span class="status-dot disconnected"></span>Can't reach the bridge</p>
          <p class="tab-note">{html.escape("; ".join(status["errors"]))}</p>
          <a class="mdl-button mdl-js-button mdl-button--raised mdl-button--colored" href="/">
            Retry
          </a>
        </div>"""
    elif status.get("logged_in"):
        body = f"""
        <div class="mdl-card mdl-shadow--2dp" style="width:100%;max-width:420px;padding:24px;">
          <p><span class="status-dot connected"></span><strong>Connected</strong></p>
          <p>{html.escape(status.get("name") or "")}</p>
          <p><code>{html.escape(status.get("jid") or "")}</code></p>
        </div>"""
    elif status.get("qr_code"):
        qr = status["qr_code"]
        src = qr if qr.startswith("data:") else f"data:image/png;base64,{qr}"
        body = f"""
        <div class="mdl-card mdl-shadow--2dp qr-card"
             style="width:100%;max-width:420px;padding:24px;">
          <p><span class="status-dot waiting"></span>Waiting for QR scan</p>
          <img id="qr-img" src="{html.escape(src, quote=True)}" alt="QR code">
          <p class="tab-note">
            Scan with WhatsApp on your phone. The bridge rotates this code
            periodically and gives up after roughly a minute unscanned — this
            page keeps itself in sync, but if it's been sitting a while, switch
            back to this tab before scanning so it's had a chance to catch up.
          </p>
        </div>
        <!-- Belt-and-suspenders fallback: a background tab's JS timers can get
             throttled or paused outright, silently leaving a dead QR on screen.
             A meta-refresh reload is safe now that / never triggers connect —
             worst case it just re-renders current server state. -->
        <meta http-equiv="refresh" content="15">
        <script>
          // Poll status only (never re-triggers connect — see
          // admin.app._poll_status) so an in-progress scan is never
          // orphaned by a page reload re-issuing a fresh QR underneath it.
          var pollTimer = null;

          function pollNow() {{
            fetch("/session/status.json")
              .then(function (r) {{ return r.json(); }})
              .then(function (s) {{
                if (s.logged_in) {{ window.location.reload(); return; }}
                if (s.qr_code) {{
                  var img = document.getElementById("qr-img");
                  if (img) {{
                    img.src = s.qr_code.indexOf("data:") === 0
                      ? s.qr_code
                      : "data:image/png;base64," + s.qr_code;
                  }}
                }} else {{
                  // The bridge killed the channel (timed out unscanned) —
                  // reload to show the "Connect WhatsApp" button rather than
                  // a dead image with no feedback.
                  window.location.reload();
                  return;
                }}
                scheduleNext();
              }})
              .catch(function () {{ scheduleNext(); }});
          }}

          function scheduleNext() {{
            if (pollTimer) clearTimeout(pollTimer);
            pollTimer = setTimeout(pollNow, 3000);
          }}

          // Catch up immediately on regaining focus, rather than waiting on a
          // timer that may have been throttled the whole time this tab was
          // backgrounded (e.g. while looking at your phone to scan).
          document.addEventListener("visibilitychange", function () {{
            if (!document.hidden) pollNow();
          }});

          scheduleNext();
        </script>"""
    else:
        body = """
        <div class="mdl-card mdl-shadow--2dp" style="width:100%;max-width:420px;padding:24px;">
          <p><span class="status-dot disconnected"></span>Not connected</p>
          <form method="post" action="/session/connect">
            <button class="mdl-button mdl-js-button mdl-button--raised mdl-button--colored"
                    type="submit">
              Connect WhatsApp
            </button>
          </form>
        </div>"""
    return _page("WhatsApp session", body, bridge_name)


def not_connected_page(bridge_name: str = "") -> str:
    body = """
    <div class="mdl-card mdl-shadow--2dp" style="width:100%;max-width:480px;padding:24px;">
      <p><span class="status-dot disconnected"></span>WhatsApp isn't connected</p>
      <p class="tab-note">
        Chat/group data reflects whatever the last connected session saw,
        which can be stale or gone — log in first so what you allow here is
        actually current.
      </p>
      <a class="mdl-button mdl-js-button mdl-button--raised mdl-button--colored" href="/">
        Go to Session
      </a>
    </div>"""
    return _page("Chats & Groups", body, bridge_name)


def _allow_toggle(jid: str, is_allowed: bool) -> str:
    jid_attr = html.escape(jid, quote=True)
    checkbox_id = f"allow-{abs(hash(jid))}"
    checked = "checked" if is_allowed else ""
    # onchange calls saveInBackground (see the page script) rather than
    # this.form.requestSubmit(): a real submit navigates to /chats and
    # forces a full page reload, which re-fetches every chat/group's
    # avatar from the WhatsApp bridge (admin/app.py chats_index) — a
    # multi-second wait for a one-row DB write. No DOM patch-up is needed
    # here the way tag edits need renderTagChips/updateRowTagData: the
    # checkbox already shows its own new state the instant it's clicked,
    # natively, so there's nothing left to update once the save is fired.
    return f"""
    <form class="allow-form" method="post" action="/chats/{jid_attr}/allow">
      <label class="mdl-checkbox mdl-js-checkbox mdl-js-ripple-effect" for="{checkbox_id}">
        <input type="checkbox" id="{checkbox_id}" class="mdl-checkbox__input"
               name="is_allowed" {checked} onchange="saveInBackground(this.form)">
        <span class="mdl-checkbox__label">Visible to MCP</span>
      </label>
    </form>"""


def _tag_chips(tags: list[str], field_id: int) -> str:
    """Saved tags as MDL deletable chip bubbles
    (https://getmdl.io/components/#chips-section) sitting above the editable
    comma-separated field below, so a chat's tags read as distinct pills
    rather than one run-together string. Each chip's action button removes
    that tag (by position — chips render in the same order as the input's
    comma-separated value, see removeTagAt below) and auto-submits the same
    form, the same instant-apply pattern the allow-toggle checkbox uses.

    The wrapping div always renders, with id="tagchips-{field_id}", even
    with zero tags (`.tag-chips:not(:empty)` in _EXTRA_CSS keeps an empty
    one from taking up space) — renderTagChips (see the page script) needs
    a stable element to redraw into after addTags/removeTagAt, whether or
    not this row had any chips at initial page load."""
    # field_id (abs(hash(jid))) can exceed Number.MAX_SAFE_INTEGER, so it's
    # passed to JS as a quoted string, not a bare numeric literal — a bare
    # literal that large gets rounded by the JS engine, and the rounded
    # value then fails to match the exact id="tags-{field_id}" string below.
    field_id_js = html.escape(str(field_id), quote=True)
    chips = "".join(
        f"""<span class="mdl-chip mdl-chip--deletable">
          <span class="mdl-chip__text">{html.escape(t)}</span>
          <button type="button" class="mdl-chip__action" title="Remove tag"
                  onclick="removeTagAt('{field_id_js}', {i})">
            <i class="material-icons">cancel</i>
          </button>
        </span>"""
        for i, t in enumerate(tags)
    )
    return f'<div class="tag-chips" id="tagchips-{field_id}">{chips}</div>'


def _metadata_form(jid: str, tags: list[str]) -> str:
    """Chips (see _tag_chips) are the only display of the saved tags — the
    field that carries them to the server (name="tags", still the same
    comma-separated string app.py's /metadata handler expects) is a hidden
    input, not a visible one. It used to be visible and pre-filled with the
    full tag list, which meant every tag appeared twice: once as a chip,
    once run together as text stuffed into a fixed-width box, truncating
    for anyone with more than a couple of short tags. The only text field
    on screen now is a small "add a tag" box — it never holds the existing
    list, so nothing to truncate — that appends into the hidden field and
    auto-submits (addTags below) on Enter or comma, the same instant-apply
    pattern chip removal (removeTagAt) and the allow-toggle checkbox
    already use. No submit button: one would only ever do what Enter
    already does, and every other control on this form is button-free for
    the same reason."""
    jid_attr = html.escape(jid, quote=True)
    field_id = abs(hash(jid))
    field_id_js = html.escape(str(field_id), quote=True)
    tags_str = ", ".join(tags)
    return f"""
    {_tag_chips(tags, field_id)}
    <form class="meta-form" method="post" action="/chats/{jid_attr}/metadata">
      <input type="hidden" id="tags-{field_id}" name="tags"
             value="{html.escape(tags_str, quote=True)}">
      <div class="mdl-textfield mdl-js-textfield">
        <input class="mdl-textfield__input" type="text" id="addtag-{field_id}"
               onkeydown="if(event.key==='Enter'||event.key===','){{
                 event.preventDefault(); addTags('{field_id_js}'); }}">
        <label class="mdl-textfield__label" for="addtag-{field_id}">Add tag…</label>
      </div>
    </form>"""


def _direct_chat_row(row: dict[str, Any]) -> str:
    jid = row["jid"]
    name = row.get("name") or jid
    tags = row.get("tags") or []
    # Both attrs feed the client-side script below: data-search backs the
    # search box (name/jid/tags, so filtering also covers tags, not just
    # the chat name), data-name backs the "sort by name" header.
    # data-tags (a JSON array — tags are free text and could contain any
    # separator character) backs the filter chip bar's toggleTagFilter.
    # data-base-search is data-search minus the tags portion — addTags/
    # removeTagAt (see the page script) rebuild data-search from it after
    # an in-place tag edit, since they update this row's tags without a
    # page reload and can't otherwise recover "just the name/jid part" of
    # an already-lowercased, already-joined data-search string.
    base_search_blob = " ".join([name, jid]).lower()
    search_blob = " ".join([base_search_blob, *(t.lower() for t in tags)]).strip()
    tags_json = html.escape(json.dumps([t.lower() for t in tags]), quote=True)
    return f"""
    <tr data-name="{html.escape(name.lower(), quote=True)}"
        data-search="{html.escape(search_blob, quote=True)}"
        data-base-search="{html.escape(base_search_blob, quote=True)}"
        data-tags="{tags_json}">
      <td>
        <div class="row-name">
          {_avatar(row.get("avatar_url"), name)}
          <div class="meta">
            <span>{html.escape(name)}</span>
            <code>{html.escape(jid)}</code>
          </div>
        </div>
      </td>
      <td>{_allow_toggle(jid, bool(row.get("is_allowed")))}</td>
      <td>{_metadata_form(jid, tags)}</td>
    </tr>"""


def _group_row(row: dict[str, Any]) -> str:
    jid = row["jid"]
    name = row.get("name") or jid
    tags = row.get("tags") or []
    topic = row.get("topic") or ""
    participant_count = row.get("participant_count", 0)
    community_badge = '<span class="badge">Community</span>' if row.get("is_community") else ""
    topic_text = html.escape(topic, quote=True) if topic else ""
    topic_div = (
        f'<div class="row-topic" title="{topic_text}">{topic_text}</div>' if topic_text else ""
    )
    # See _direct_chat_row for data-search/data-base-search/data-name/
    # data-tags; data-members backs the "sort by member count" header
    # (topic included in the search blob too — searching a group's
    # description is useful, same as tags — and in the base, since a
    # group's topic doesn't change from in-place tag edits either).
    base_search_blob = " ".join([name, jid, topic]).lower()
    search_blob = " ".join([base_search_blob, *(t.lower() for t in tags)]).strip()
    tags_json = html.escape(json.dumps([t.lower() for t in tags]), quote=True)
    return f"""
    <tr data-name="{html.escape(name.lower(), quote=True)}"
        data-members="{participant_count}"
        data-search="{html.escape(search_blob, quote=True)}"
        data-base-search="{html.escape(base_search_blob, quote=True)}"
        data-tags="{tags_json}">
      <td>
        <div class="row-name">
          {_avatar(row.get("avatar_url"), name)}
          <div class="meta">
            <span>{html.escape(name)}{community_badge}</span>
            <code>{html.escape(jid)}</code>
            {topic_div}
          </div>
        </div>
      </td>
      <td>{participant_count} members</td>
      <td>{_allow_toggle(jid, bool(row.get("is_allowed")))}</td>
      <td>{_metadata_form(jid, tags)}</td>
    </tr>"""


def _distinct_tags(rows: list[dict[str, Any]]) -> list[str]:
    """Unique tags in use across `rows`, alphabetical (case-insensitive),
    keeping each tag's first-seen casing. Feeds the filter chip bar below —
    only tags actually applied to something in this table are offered."""
    seen: dict[str, str] = {}
    for row in rows:
        for tag in row.get("tags") or []:
            seen.setdefault(tag.lower(), tag)
    return sorted(seen.values(), key=str.lower)


def _filter_chip_bar(tags: list[str], table_id: str, search_input_id: str) -> str:
    """Filter chips above a table — the Material Design "filter chip" pattern
    (https://m2.material.io/components/chips#filter-chips): a row of
    multi-select chips, each toggling whether rows carrying that tag are
    shown. Selected chips OR together (a row shows if it has *any* selected
    tag) — the usual semantics for multi-select within one facet, and it
    composes with the free-text search box via toggleTagFilter/
    applyTableFilter below rather than replacing it."""
    if not tags:
        return ""
    chips = "".join(
        f"""<span class="mdl-chip mdl-chip--filter" tabindex="0" role="button"
                  aria-pressed="false" data-tag="{html.escape(t.lower(), quote=True)}"
                  onclick="toggleTagFilter(this, '{table_id}')"
                  onkeydown="if(event.key===' '||event.key==='Enter'){{
                    event.preventDefault(); this.click(); }}">
          <i class="material-icons filter-chip-check">check</i>
          <span class="mdl-chip__text">{html.escape(t)}</span>
        </span>"""
        for t in tags
    )
    return (
        f'<div class="filter-chip-bar" id="{table_id}-filters" '
        f'data-search-input="{html.escape(search_input_id, quote=True)}">{chips}</div>'
    )


def _empty_row(colspan: int, message: str) -> str:
    return (
        f'<tr><td colspan="{colspan}"><p class="empty-state">{html.escape(message)}</p></td></tr>'
    )


def browse_page(
    direct_rows: list[dict[str, Any]],
    group_rows: list[dict[str, Any]],
    status_row: dict[str, Any] | None,
    bridge_name: str = "",
) -> str:
    direct_trs = "".join(_direct_chat_row(r) for r in direct_rows) or _empty_row(
        3, "No direct chats yet — give WhatsApp a moment to sync after logging in."
    )
    group_trs = "".join(_group_row(r) for r in group_rows) or _empty_row(
        4, "No groups found — the connected account isn't in any groups yet."
    )

    # Some bridges (WAHA, confirmed live) never surface a status@broadcast
    # chat at all — there's nothing to toggle or show, so the tab itself
    # is dead weight rather than a legitimate "nothing yet" empty state.
    # Only render it once there's an actual status_row to act on.
    status_tab = ""
    status_panel = ""
    if status_row:
        status_jid = status_row["jid"]
        status_tags = status_row.get("tags") or []
        status_tab = '<a href="#status-panel" class="mdl-tabs__tab">Status</a>'
        status_panel = f"""
      <div class="mdl-tabs__panel" id="status-panel">
        <p class="tab-note">
          WhatsApp's Status/Stories updates — a single feed, not a chat with a
          person or group. Most people leave this hidden.
        </p>
        {_allow_toggle(status_jid, bool(status_row.get("is_allowed")))}
        {_metadata_form(status_jid, status_tags)}
      </div>"""

    body = f"""
    <p class="tab-note">
      Toggling "Visible to MCP" is the actual privacy boundary — Claude cannot see a
      chat, its messages, or its metadata until it's checked here.
    </p>
    <div class="mdl-tabs mdl-js-tabs mdl-js-ripple-effect">
      <div class="mdl-tabs__tab-bar">
        <a href="#direct-panel" class="mdl-tabs__tab is-active">Chats</a>
        <a href="#groups-panel" class="mdl-tabs__tab">Groups</a>
        {status_tab}
      </div>

      <div class="mdl-tabs__panel is-active" id="direct-panel">
        <div class="table-toolbar mdl-textfield mdl-js-textfield">
          <input class="mdl-textfield__input" type="search" id="direct-search"
                 oninput="applyTableFilter('direct-search', 'direct-table')">
          <label class="mdl-textfield__label" for="direct-search">
            Search chats (name, tags)…
          </label>
        </div>
        {_filter_chip_bar(_distinct_tags(direct_rows), "direct-table", "direct-search")}
        <div class="table-scroll">
        <table id="direct-table" class="mdl-data-table mdl-js-data-table" style="width:100%;">
          <thead><tr>
            <th data-sort-key="name" data-sort-type="text"
                onclick="sortTable(this, 'direct-table')">Chat</th>
            <th>Allowlist</th>
            <th>Tags</th>
          </tr></thead>
          <tbody>{direct_trs}</tbody>
        </table>
        </div>
      </div>

      <div class="mdl-tabs__panel" id="groups-panel">
        <div class="table-toolbar mdl-textfield mdl-js-textfield">
          <input class="mdl-textfield__input" type="search" id="groups-search"
                 oninput="applyTableFilter('groups-search', 'groups-table')">
          <label class="mdl-textfield__label" for="groups-search">
            Search groups (name, topic, tags)…
          </label>
        </div>
        {_filter_chip_bar(_distinct_tags(group_rows), "groups-table", "groups-search")}
        <div class="table-scroll">
        <table id="groups-table" class="mdl-data-table mdl-js-data-table" style="width:100%;">
          <thead><tr>
            <th data-sort-key="name" data-sort-type="text"
                onclick="sortTable(this, 'groups-table')">Group</th>
            <th data-sort-key="members" data-sort-type="number"
                onclick="sortTable(this, 'groups-table')">Members</th>
            <th>Allowlist</th><th>Tags</th>
          </tr></thead>
          <tbody>{group_trs}</tbody>
        </table>
        </div>
      </div>

      {status_panel}
    </div>
    <script>
      // Restore the active tab from the URL fragment — the allowlist/
      // tags forms below redirect back to /chats#<panel-id> after a save
      // so ticking a checkbox on the Groups tab doesn't flip the page
      // back to Chats. Sets the same "is-active" classes MDL's own tab
      // click handler would, directly, rather than depending on MDL's
      // upgrade JS having already run by this point.
      (function () {{
        var hash = location.hash;
        if (!hash) return;
        var panel = document.querySelector(hash);
        var tab = document.querySelector('a[href="' + hash + '"]');
        if (!panel || !tab) return;
        document.querySelectorAll(".mdl-tabs__panel").forEach(function (p) {{
          p.classList.remove("is-active");
        }});
        document.querySelectorAll(".mdl-tabs__tab").forEach(function (t) {{
          t.classList.remove("is-active");
        }});
        panel.classList.add("is-active");
        tab.classList.add("is-active");
      }})();

      // Redraws one row's chips (see _tag_chips) from an in-memory tags
      // array — called by addTags/removeTagAt below after they change
      // that row's tags, so the chip pile updates the instant you hit
      // Enter/comma or the chip's own delete button, rather than waiting
      // on saveTags' round trip. Chip text uses textContent (not the
      // string-built markup _tag_chips renders server-side) so a tag
      // containing "<" or "&" can't be parsed as markup here.
      function renderTagChips(fieldId, tags) {{
        var container = document.getElementById("tagchips-" + fieldId);
        if (!container) return;
        container.innerHTML = "";
        tags.forEach(function (t, i) {{
          var chip = document.createElement("span");
          chip.className = "mdl-chip mdl-chip--deletable";
          var text = document.createElement("span");
          text.className = "mdl-chip__text";
          text.textContent = t;
          chip.appendChild(text);
          var btn = document.createElement("button");
          btn.type = "button";
          btn.className = "mdl-chip__action";
          btn.title = "Remove tag";
          btn.onclick = function () {{ removeTagAt(fieldId, i); }};
          var icon = document.createElement("i");
          icon.className = "material-icons";
          icon.textContent = "cancel";
          btn.appendChild(icon);
          chip.appendChild(btn);
          container.appendChild(chip);
        }});
      }}

      // Keeps a row's data-search/data-tags (see _direct_chat_row) in sync
      // with its tags after an in-place edit, so the search box and filter
      // chip bar's "does this row match" checks (applyTableFilter below)
      // stay correct without a page reload. Doesn't add a brand-new tag to
      // the filter chip bar itself — that bar is built once at page load
      // from every row's tags (_distinct_tags); a tag typed in since then
      // is still searchable by text immediately; it shows up as its own
      // filter chip after the next reload.
      function updateRowTagData(fieldId, tags) {{
        var hidden = document.getElementById("tags-" + fieldId);
        var row = hidden ? hidden.closest("tr") : null;
        if (!row) return;
        var tagsLower = tags.map(function (t) {{ return t.toLowerCase(); }});
        row.dataset.tags = JSON.stringify(tagsLower);
        row.dataset.search = (row.dataset.baseSearch + " " + tagsLower.join(" ")).trim();
      }}

      // Submits any of this page's small forms (the allow-toggle checkbox,
      // the tags form) in the background instead of navigating. A plain
      // form.requestSubmit() here would navigate to /chats#<panel-id> and
      // force a full page reload — which re-fetches every chat/group's
      // avatar from the WhatsApp bridge (see admin/app.py chats_index) —
      // turning a one-row DB write into a multi-second wait. Whatever DOM
      // update the caller needs (renderTagChips/updateRowTagData for tags;
      // the checkbox needs none, it already shows its new state natively)
      // is expected to have happened before this is called, so the actual
      // save can happen silently; fetch's own errors just get logged
      // rather than shown, same as this page's other background calls
      // (session_page's pollNow).
      function saveInBackground(form) {{
        fetch(form.action, {{method: "POST", body: new FormData(form)}})
          .catch(function (err) {{ console.error("Failed to save:", err); }});
      }}

      // Tags-form-specific wrapper around saveInBackground — addTags/
      // removeTagAt below know the row's fieldId, not its <form> element.
      function saveTags(fieldId) {{
        var hidden = document.getElementById("tags-" + fieldId);
        if (!hidden) return;
        saveInBackground(hidden.form);
      }}

      // A tag chip's delete action (see _tag_chips): drop the tag at
      // `idx` from that row's comma-separated tags field (a hidden input,
      // see _metadata_form), by position — chips render in the same order
      // as the field's value.
      function removeTagAt(fieldId, idx) {{
        var input = document.getElementById("tags-" + fieldId);
        if (!input) return;
        var tags = input.value.split(",").map(function (s) {{ return s.trim(); }})
          .filter(function (s) {{ return s; }});
        tags.splice(idx, 1);
        input.value = tags.join(", ");
        renderTagChips(fieldId, tags);
        updateRowTagData(fieldId, tags);
        saveTags(fieldId);
      }}

      // The "Add tag…" box's Enter/comma action (see _metadata_form): read
      // the typed text (comma-separated, so pasting "a, b" adds both),
      // merge it into the row's hidden tags field case-insensitively
      // deduped against what's already there, and clear the box.
      function addTags(fieldId) {{
        var addInput = document.getElementById("addtag-" + fieldId);
        var hidden = document.getElementById("tags-" + fieldId);
        if (!addInput || !hidden) return;
        var newTags = addInput.value.split(",").map(function (s) {{ return s.trim(); }})
          .filter(function (s) {{ return s; }});
        if (!newTags.length) return;
        var tags = hidden.value.split(",").map(function (s) {{ return s.trim(); }})
          .filter(function (s) {{ return s; }});
        var seen = {{}};
        tags.forEach(function (t) {{ seen[t.toLowerCase()] = true; }});
        newTags.forEach(function (t) {{
          if (!seen[t.toLowerCase()]) {{
            tags.push(t);
            seen[t.toLowerCase()] = true;
          }}
        }});
        hidden.value = tags.join(", ");
        addInput.value = "";
        renderTagChips(fieldId, tags);
        updateRowTagData(fieldId, tags);
        saveTags(fieldId);
      }}

      // Live search + filter chips + column sort for the Chats/Groups
      // tables — pure client-side (every row is already on the page; no
      // round trip). Rows without data-search are the "nothing here yet"
      // empty-state row, deliberately excluded from all three.

      // A filter chip's click (see _filter_chip_bar): toggle its selected
      // state — the Material "filter chip" pattern
      // (m2.material.io/components/chips#filter-chips) — then re-apply.
      function toggleTagFilter(chip, tableId) {{
        var active = chip.classList.toggle("is-active");
        chip.setAttribute("aria-pressed", active ? "true" : "false");
        var bar = document.getElementById(tableId + "-filters");
        applyTableFilter(bar ? bar.dataset.searchInput : null, tableId);
      }}

      // Combines the free-text search box with any selected filter chips:
      // a row must match the search term AND carry at least one selected
      // tag (selected tags themselves OR together — see _filter_chip_bar).
      function applyTableFilter(inputId, tableId) {{
        var input = inputId ? document.getElementById(inputId) : null;
        var term = input ? input.value.trim().toLowerCase() : "";
        var activeTags = Array.prototype.slice.call(
          document.querySelectorAll("#" + tableId + "-filters .mdl-chip--filter.is-active")
        ).map(function (chip) {{ return chip.dataset.tag; }});
        document.querySelectorAll("#" + tableId + " tbody tr[data-search]").forEach(function (r) {{
          var matchesSearch = !term || r.dataset.search.indexOf(term) !== -1;
          var matchesTags = true;
          if (activeTags.length) {{
            var rowTags = JSON.parse(r.dataset.tags || "[]");
            matchesTags = activeTags.some(function (tag) {{ return rowTags.indexOf(tag) !== -1; }});
          }}
          r.style.display = matchesSearch && matchesTags ? "" : "none";
        }});
      }}

      function sortTable(th, tableId) {{
        var table = document.getElementById(tableId);
        var tbody = table.querySelector("tbody");
        var rows = Array.prototype.slice.call(tbody.querySelectorAll("tr[data-search]"));
        if (!rows.length) return;
        var key = th.dataset.sortKey;
        var type = th.dataset.sortType;
        var dir = th.dataset.sortDir === "asc" ? "desc" : "asc";
        table.querySelectorAll("th[data-sort-key]").forEach(function (h) {{
          delete h.dataset.sortDir;
          h.classList.remove("sort-asc", "sort-desc");
        }});
        th.dataset.sortDir = dir;
        th.classList.add(dir === "asc" ? "sort-asc" : "sort-desc");
        rows.sort(function (a, b) {{
          var av = a.dataset[key];
          var bv = b.dataset[key];
          if (type === "number") {{
            av = parseFloat(av) || 0;
            bv = parseFloat(bv) || 0;
          }}
          if (av < bv) return dir === "asc" ? -1 : 1;
          if (av > bv) return dir === "asc" ? 1 : -1;
          return 0;
        }});
        rows.forEach(function (r) {{ tbody.appendChild(r); }});
      }}
    </script>
    """
    return _page("Chats & Groups", body, bridge_name)


def _allowed_email_row(email: str) -> str:
    email_attr = html.escape(email, quote=True)
    return f"""
    <tr>
      <td>{html.escape(email)}</td>
      <td>
        <form method="post" action="/access/{email_attr}/remove"
              onsubmit="return confirm('Remove {email_attr} from the allowlist?');">
          <button class="mdl-button mdl-js-button mdl-button--icon" type="submit" title="Remove">
            <i class="material-icons">delete</i>
          </button>
        </form>
      </td>
    </tr>"""


def access_page(emails: list[str], bridge_name: str = "") -> str:
    """Who may authenticate to the MCP server at all — see
    docs/decisions/0015-email-allowlist-in-db.md. Distinct from the chat
    allowlist on /chats, which gates what an already-authenticated caller
    can see, not who can log in."""
    rows = (
        "".join(_allowed_email_row(e) for e in emails)
        if emails
        else '<tr><td colspan="2" class="empty-state">No emails allowed yet — '
        "nobody can use the MCP server until you add one.</td></tr>"
    )
    body = f"""
    <p class="tab-note">
      Only these emails may authenticate to the MCP server — this is who can
      log in at all, separate from which chats a logged-in caller can see
      (that's the Chats &amp; Groups tab). An AuthKit login from an email not
      listed here is rejected after sign-in.
    </p>
    <div class="mdl-card mdl-shadow--2dp" style="width:100%;max-width:480px;padding:24px;">
      <form method="post" action="/access/add" style="display:flex;gap:12px;align-items:flex-end;">
        <div class="mdl-textfield mdl-js-textfield" style="flex:1;">
          <input class="mdl-textfield__input" type="email" name="email" id="new-email" required>
          <label class="mdl-textfield__label" for="new-email">Email to allow…</label>
        </div>
        <button class="mdl-button mdl-js-button mdl-button--raised mdl-button--colored"
                type="submit">
          Add
        </button>
      </form>
    </div>
    <div class="table-scroll">
      <table class="mdl-data-table mdl-js-data-table" style="width:100%;max-width:480px;">
        <thead><tr><th>Email</th><th></th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>"""
    return _page("Access", body, bridge_name)
