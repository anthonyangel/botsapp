"""
HTML templates for the admin UI, styled with Web Awesome (docs/decisions/0032)
loaded as ES modules from its CDN — rather than hand-rolling a design system
for an internal tool.

Still no template engine dependency and no build step: Web Awesome supplies the
components, layout utilities and tokens; f-strings + html.escape still assemble
the markup. Every value that originates from WhatsApp (chat/group names,
topics) is untrusted and passed through html.escape before landing in markup.
"""

import html
import json
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from botsapp.admin.prefix import p

_WA_VERSION = "3.14.0"
_WA_BASE = f"https://ka-f.webawesome.com/webawesome@{_WA_VERSION}"
_WA_CSS = f"{_WA_BASE}/styles/webawesome.css"
_WA_JS = f"{_WA_BASE}/webawesome.loader.js"
# Subresource Integrity for the loader only: the CDN sends no CORS headers for
# the stylesheet, so a stylesheet with integrity= is blocked outright. SRI also
# doesn't cover the chunks the loader imports at runtime (0032, Consequences).
_WA_JS_SRI = "sha384-yZsihvrX2pSJAWAKNonC0DyMt6ZKuICXhS7gNOt+K0o3K/8BWgWwob6PTIxodq/c"

_EXTRA_CSS = """
  html, body { min-height: 100%; padding: 0; margin: 0; }
  wa-page { --menu-width: 15rem; }
  wa-page[view='mobile'] { --menu-width: auto; }
  main { max-width: 64rem; }
  main h1 { font-size: var(--wa-font-size-2xl); margin: 0; }
  main h2 { font-size: var(--wa-font-size-l); margin: 0; }
  wa-page[view='mobile'] main { padding: var(--wa-space-m); }
  .nav-link[aria-current='page'] { font-weight: var(--wa-font-weight-bold); }
  .note { color: var(--wa-color-text-quiet); font-size: var(--wa-font-size-s); margin: 0; }
  .empty-state { color: var(--wa-color-text-quiet); padding: var(--wa-space-l) 0;
                 text-align: center; }
  .row-name { min-width: 0; max-width: 20rem; }
  .row-name .meta { min-width: 0; }
  .row-name .meta > * { white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
                        max-width: 100%; }
  .row-name code, .row-topic { font-size: var(--wa-font-size-xs);
                               color: var(--wa-color-text-quiet); }
  .table-toolbar { max-width: 24rem; }
  .tag-list:empty { display: none; }
  .meta-form wa-input { min-width: 8rem; }
  .data-table { width: 100%; border-collapse: collapse; }
  .data-table th { text-align: start; color: var(--wa-color-text-quiet);
                   font-weight: var(--wa-font-weight-semibold); }
  .data-table th, .data-table td {
    padding: var(--wa-space-s) var(--wa-space-m); vertical-align: middle;
    border-bottom: var(--wa-border-width-s) var(--wa-border-style) var(--wa-color-surface-border); }
  th[data-sort-key] { cursor: pointer; user-select: none; }
  th[data-sort-key]:hover { text-decoration: underline; }
  th.sort-asc::after { content: " \\25B2"; font-size: var(--wa-font-size-2xs); }
  th.sort-desc::after { content: " \\25BC"; font-size: var(--wa-font-size-2xs); }
  .qr-card img { width: 15rem; height: 15rem; border-radius: var(--wa-border-radius-m);
                 display: block; margin: var(--wa-space-m) auto; }
  .account-card { max-width: 40rem; }
  .inline-form { display: inline; margin: 0; }
  .row-name wa-avatar { --size: 2.25rem; }
  /* Phones: each table row becomes a card, so nothing scrolls sideways. The
     header row (and with it column sorting) is hidden; cells name themselves
     via data-label. */
  @media (max-width: 640px) {
    .data-table thead { display: none; }
    .data-table, .data-table tbody, .data-table tr, .data-table td { display: block; }
    .data-table tr { padding: var(--wa-space-m); margin-block-end: var(--wa-space-s);
      background: var(--wa-color-surface-raised); border-radius: var(--wa-border-radius-l);
      border: var(--wa-border-width-s) var(--wa-border-style) var(--wa-color-surface-border); }
    .data-table td { padding: var(--wa-space-2xs) 0; border: 0; }
    .data-table td[data-label]::before { content: attr(data-label); display: block;
      font-size: var(--wa-font-size-s); color: var(--wa-color-text-quiet); }
    .row-name { max-width: none; }
  }
"""


@dataclass(frozen=True)
class NavContext:
    """Who is looking and which account they're acting on, set per request by
    admin/auth.py's middleware so every page's header can reflect it without
    each page function growing parameters. The default shows everything."""

    email: str = ""
    is_admin: bool = True
    accounts: tuple[tuple[str, str], ...] = ()  # (id, label) the user can select
    account: str = ""


@dataclass(frozen=True)
class ChatView:
    """How the Chats page is being used, set by its handler for the render.

    ``for_viewer`` is empty when you are editing your own view; set to a
    granted viewer's email when the account's owner is choosing what *that*
    person can see (then the URLs carry ``?viewer=``). ``can_toggle`` is the
    "Visible to MCP" switch, which only the account's owner has; ``can_tag`` is
    editing your own tags."""

    can_toggle: bool = True
    can_tag: bool = True
    for_viewer: str = ""
    me: str = ""
    owner: str = ""
    viewers: tuple[str, ...] = ()  # the owner's choices: themself first, then grants


_DEFAULT_CHAT_VIEW = ChatView()  # frozen, so safe as a shared default
chat_view: ContextVar[ChatView] = ContextVar("admin_chat_view", default=_DEFAULT_CHAT_VIEW)


def _chat_url(jid: str, op: str) -> str:
    base = f"{p('/chats')}/{html.escape(jid, quote=True)}/{op}"
    viewer = chat_view.get().for_viewer
    return f"{base}?viewer={html.escape(quote(viewer, safe='@'), quote=True)}" if viewer else base


def _chat_header() -> str:
    view = chat_view.get()
    if view.viewers and len(view.viewers) > 1:
        options = "".join(
            f'<option value="{html.escape(v, quote=True)}"'
            f"{' selected' if v == (view.for_viewer or view.me) else ''}>"
            f"{html.escape(v)}{' (you)' if v == view.me else ''}</option>"
            for v in view.viewers
        )
        who = view.for_viewer or view.me
        note = (
            "You are choosing what <b>" + html.escape(who) + "</b> can see on this account."
            if view.for_viewer
            else "These are the chats <b>you</b> can see. Pick someone to choose what they see."
        )
        return f"""
    <p class="note">
      <label>Showing visibility for
        <select aria-label="Viewer"
                onchange="location.href='{p("/chats")}?viewer='+encodeURIComponent(this.value)">
          {options}</select></label><br>{note}
    </p>"""
    if not view.can_toggle:
        return (
            '<p class="note">These are the chats '
            f"<b>{html.escape(view.owner)}</b> has shared with you. You can label them for "
            "yourself; only they can change what is shared.</p>"
        )
    return ""


_DEFAULT_NAV = NavContext()  # frozen, so safe as a shared default
nav: ContextVar[NavContext] = ContextVar("admin_nav", default=_DEFAULT_NAV)


def _select_account_form(account_id: str, *, label: str, next_url: str | None = None) -> str:
    """A POST form that selects `account_id`, rendered as a plain button.

    `/accounts/<id>/select` mutates the botsapp_account cookie, so it must be a
    POST (AuthMiddleware's CSRF/Origin check only applies to non-GET/HEAD/OPTIONS
    requests) rather than an `<a href>`/`location.href` navigation.
    """
    aid = html.escape(quote(account_id, safe=""), quote=True)
    action = f"{p('/accounts')}/{aid}/select"
    if next_url:
        action += f"?next={html.escape(quote(next_url, safe=''), quote=True)}"
    return (
        f'<form method="post" action="{action}" class="inline-form">'
        f'<wa-button type="submit" appearance="plain" variant="brand" size="s">'
        f"{html.escape(label)}</wa-button></form>"
    )


_NAV_ITEMS = (("Session", "/"), ("Chats & Groups", "/chats"), ("Users", "/users"))


def _nav_html(active: str = "") -> str:
    """Account switcher plus nav links, written once into <wa-page>'s
    ``navigation`` slot (the component renders it as the desktop sidebar or
    the mobile drawer). ``active`` is the current page title."""
    ctx = nav.get()
    links = []
    for title, path in _NAV_ITEMS:
        if path == "/users" and not ctx.is_admin:
            continue
        current = ' aria-current="page"' if title == active else ""
        links.append(
            f'<a class="nav-link" data-drawer="close"{current} href="{p(path)}">'
            f"{html.escape(title)}</a>"
        )
    links.append(f'<a class="nav-link" data-drawer="close" href="{p("/logout")}">Sign out</a>')
    switcher = ""
    if len(ctx.accounts) > 1:
        options = "".join(
            f'<option value="{html.escape(i, quote=True)}"'
            f"{' selected' if i == ctx.account else ''}>{html.escape(label)}</option>"
            for i, label in ctx.accounts
        )
        switcher = (
            '<form method="post">'
            '<select aria-label="Account" '
            f"onchange=\"this.form.action='{p('/accounts')}/'+encodeURIComponent(this.value)"
            "+'/select'; this.form.submit();\">"
            f"{options}</select></form>"
        )
    return switcher + "".join(links)


def _page(title: str, body: str, bridge_name: str = "") -> str:
    bridge_badge = (
        f'<wa-badge variant="brand">{html.escape(bridge_name.upper())}</wa-badge>'
        if bridge_name
        else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en" class="wa-theme-default wa-palette-default wa-light">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <link rel="stylesheet" href="{_WA_CSS}">
  <script type="module" src="{_WA_JS}" integrity="{_WA_JS_SRI}" crossorigin="anonymous"></script>
  <style>{_EXTRA_CSS}</style>
</head>
<body>
  <wa-page mobile-breakpoint="768">
    <header slot="header" class="wa-cluster wa-gap-s">
      <wa-button data-toggle-nav appearance="plain" class="wa-mobile-only" aria-label="Open menu">
        <wa-icon name="bars"></wa-icon>
      </wa-button>
      <strong>WhatsApp Admin</strong>
      {bridge_badge}
    </header>
    <nav slot="navigation" class="wa-stack wa-gap-s">{_nav_html(title)}</nav>
    <main class="wa-stack wa-gap-l">
      <h1>{html.escape(title)}</h1>
      {body}
    </main>
  </wa-page>
</body>
</html>"""


def message_page(title: str, message_html: str) -> str:
    """A bare notice page (login errors, signed-out). ``message_html`` is
    trusted markup from admin/auth.py; interpolate user data only via
    html.escape before passing it in."""
    return _page(title, f"<wa-card>{message_html}</wa-card>")


def _avatar(url: str | None, alt: str) -> str:
    if url:
        src = html.escape(url, quote=True)
        return f'<wa-avatar image="{src}" label="{html.escape(alt, quote=True)}"></wa-avatar>'
    return f'<wa-avatar label="{html.escape(alt, quote=True)}"></wa-avatar>'


def _pairing_panel(account_id: str, status: dict[str, Any]) -> str:
    """Live Connect/QR/Disconnect controls for one account's card (0029:
    folded in from the old standalone Session page, which showed this for
    only whichever account happened to be selected). ``status``:
    SessionStatus as a dict (see bridge.py) — the shape
    accounts._account_status() builds via dataclasses.asdict(). Poll-only
    (see that function's docstring for why this matters): loading or
    refreshing this card never triggers a WhatsApp connect attempt on its
    own.

    Each card's QR-poll script is wrapped in its own closure (keyed by
    ``account_id``) so two accounts mid-pairing on the same page don't
    collide over a shared ``pollTimer``/img element id.
    """
    aid = html.escape(quote(account_id, safe=""), quote=True)
    base = f"{p('/accounts')}/{aid}/session"
    if status.get("errors"):
        return f"""
      <div class="pairing-panel wa-stack wa-gap-s">
        <p><wa-badge variant="danger">Can't reach the bridge</wa-badge></p>
        <p class="note">{html.escape("; ".join(status["errors"]))}</p>
      </div>"""
    if status.get("logged_in"):
        name_html = html.escape(status.get("name") or "")
        jid_html = html.escape(status.get("jid") or "")
        # Show name/jid only when WAHA actually returned them (a truly live
        # session); a stale persisted session can appear WORKING but have
        # no me.id / pushName yet because the phone is unreachable.
        identity_html = (
            f"<p>{name_html}</p><p><code>{jid_html}</code></p>"
            if name_html or jid_html
            else (
                '<p class="note">No account details yet — the session may be '
                "stale. Use the buttons below to re-pair.</p>"
            )
        )
        return f"""
      <div class="pairing-panel wa-stack wa-gap-s">
        <p><wa-badge variant="success">Connected</wa-badge></p>
        {identity_html}
        <div class="wa-cluster wa-gap-s">
          <form method="post" action="{base}/disconnect">
            <wa-button type="submit" appearance="outlined"
                    title="Log out gracefully — session stays in STOPPED state; click Connect
                           WhatsApp to re-pair">
              Disconnect
            </wa-button>
          </form>
          <form method="post" action="{base}/reset"
                onsubmit="return confirm(
                  'Delete the saved session entirely and force a fresh QR scan?')">
            <wa-button type="submit" appearance="outlined" variant="danger"
                    title="Delete all saved auth material and force a fresh QR scan">
              Force re-pair
            </wa-button>
          </form>
        </div>
      </div>"""
    if status.get("qr_code"):
        qr = status["qr_code"]
        src = qr if qr.startswith("data:") else f"data:image/png;base64,{qr}"
        img_id = f"qr-img-{aid}"
        return f"""
      <div class="pairing-panel qr-card wa-stack wa-gap-s">
        <p><wa-badge variant="warning">Waiting for QR scan</wa-badge></p>
        <img id="{img_id}" src="{html.escape(src, quote=True)}" alt="QR code">
        <p class="note">
          Scan with WhatsApp on your phone. The bridge rotates this code
          periodically and gives up after roughly a minute unscanned — this
          card keeps itself in sync, but if it's been sitting a while, switch
          back to this tab before scanning so it's had a chance to catch up.
        </p>
        <script>
        (function () {{
          // Poll status only (never re-triggers connect — see
          // accounts._account_status) so an in-progress scan is never
          // orphaned by a page reload re-issuing a fresh QR underneath it.
          var pollTimer = null;

          function pollNow() {{
            fetch("{base}/status.json")
              .then(function (r) {{ return r.json(); }})
              .then(function (s) {{
                if (s.logged_in) {{ window.location.reload(); return; }}
                if (s.qr_code) {{
                  var img = document.getElementById("{img_id}");
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
        }})();
        </script>
      </div>"""
    return f"""
      <div class="pairing-panel wa-stack wa-gap-s">
        <p><wa-badge variant="neutral">Not connected</wa-badge></p>
        <form method="post" action="{base}/connect">
          <wa-button type="submit" variant="brand">
            Connect WhatsApp
          </wa-button>
        </form>
      </div>"""


def not_connected_page(bridge_name: str = "") -> str:
    body = f"""
    <wa-card class="account-card">
      <div class="wa-stack wa-gap-m">
        <p><wa-badge variant="danger">WhatsApp isn't connected</wa-badge></p>
        <p class="note">
          Chat/group data reflects whatever the last connected session saw,
          which can be stale or gone — log in first so what you allow here is
          actually current.
        </p>
        <div><wa-button variant="brand" href="{p("/")}">Go to Session</wa-button></div>
      </div>
    </wa-card>"""
    return _page("Chats & Groups", body, bridge_name)


def _allow_toggle(jid: str, is_allowed: bool) -> str:
    if not chat_view.get().can_toggle:
        return '<wa-badge variant="neutral">Shared with you</wa-badge>'
    checked = "checked" if is_allowed else ""
    # onchange calls saveInBackground (see the page script) rather than
    # submitting the form: a real submit navigates to /chats and forces a
    # full page reload, which re-fetches every chat/group's avatar from the
    # WhatsApp bridge (admin/app.py chats_index) — a multi-second wait for a
    # one-row DB write. No DOM patch-up is needed here the way tag edits need
    # renderTagChips/updateRowTagData: the switch already shows its own new
    # state the instant it's clicked, so there's nothing left to update once
    # the save is fired.
    return f"""
    <form class="allow-form" method="post" action="{_chat_url(jid, "allow")}">
      <wa-switch name="is_allowed" {checked}
                 onchange="saveInBackground(this.closest('form'))">Visible to MCP</wa-switch>
    </form>"""


def _tag_chips(tags: list[str], field_id: int, removable: bool = True) -> str:
    """Saved tags as removable <wa-tag> pills sitting above the "add a tag"
    box below, so a chat's tags read as distinct pills rather than one
    run-together string. A tag's remove button (wa-remove event, handled by
    the page script's delegated listener) drops it by position — tags render
    in the same order as the hidden field's comma-separated value — and
    auto-submits the same form, the same instant-apply pattern the allow
    switch uses.

    The wrapping div always renders, with id="tagchips-{field_id}", even with
    zero tags (`.tag-list:empty` in _EXTRA_CSS keeps an empty one from taking
    up space) — renderTagChips (see the page script) needs a stable element to
    redraw into after addTags/removeTagAt, whether or not this row had any
    tags at initial page load."""
    remove = " with-remove" if removable else ""
    chips = "".join(
        f'<wa-tag size="s"{remove} data-idx="{i}">{html.escape(t)}</wa-tag>'
        for i, t in enumerate(tags)
    )
    return (
        f'<div class="tag-list wa-cluster wa-gap-2xs" id="tagchips-{field_id}" '
        f'data-field="{field_id}">{chips}</div>'
    )


def _metadata_form(jid: str, tags: list[str]) -> str:
    """Tag pills (see _tag_chips) are the only display of the saved tags — the
    field that carries them to the server (name="tags", still the same
    comma-separated string app.py's /metadata handler expects) is a hidden
    input, not a visible one. The only text field on screen is a small "add a
    tag" box that never holds the existing list; it appends into the hidden
    field and auto-submits (addTags in the page script) on Enter or comma, the
    same instant-apply pattern tag removal and the allow switch use. No submit
    button: one would only ever do what Enter already does."""
    field_id = abs(hash(jid))
    field_id_js = html.escape(str(field_id), quote=True)
    tags_str = ", ".join(tags)
    if not chat_view.get().can_tag:
        # Tags are each person's own labels; the owner choosing what someone
        # else sees doesn't edit theirs.
        return _tag_chips(tags, field_id, removable=False)
    return f"""
    <div class="wa-stack wa-gap-xs">
      {_tag_chips(tags, field_id)}
      <form class="meta-form" method="post" action="{_chat_url(jid, "metadata")}">
        <input type="hidden" id="tags-{field_id}" name="tags"
               value="{html.escape(tags_str, quote=True)}">
        <wa-input size="s" id="addtag-{field_id}" placeholder="Add tag…" aria-label="Add tag"
                  onkeydown="if(event.key==='Enter'||event.key===','){{
                    event.preventDefault(); addTags('{field_id_js}'); }}"></wa-input>
      </form>
    </div>"""


def _direct_chat_row(row: dict[str, Any]) -> str:
    jid = row["jid"]
    name = row.get("name") or jid
    tags = row.get("tags") or []
    country_flag = row.get("country_flag") or ""
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
    # data-country backs the "sort by country" header — sorting on the
    # rendered "🇮🇱 +972" text groups by flag first, which is what you'd
    # expect from a country column.
    base_search_blob = " ".join([name, jid]).lower()
    search_blob = " ".join([base_search_blob, *(t.lower() for t in tags)]).strip()
    tags_json = html.escape(json.dumps([t.lower() for t in tags]), quote=True)
    return f"""
    <tr data-name="{html.escape(name.lower(), quote=True)}"
        data-country="{html.escape(country_flag, quote=True)}"
        data-search="{html.escape(search_blob, quote=True)}"
        data-base-search="{html.escape(base_search_blob, quote=True)}"
        data-tags="{tags_json}">
      <td>
        <div class="row-name wa-flank wa-gap-s">
          {_avatar(row.get("avatar_url"), name)}
          <div class="meta wa-stack wa-gap-3xs">
            <span>{html.escape(name)}</span>
            <code>{html.escape(jid)}</code>
          </div>
        </div>
      </td>
      <td data-label="Country">{html.escape(country_flag)}</td>
      <td data-label="Allowlist">{_allow_toggle(jid, bool(row.get("is_allowed")))}</td>
      <td data-label="Tags">{_metadata_form(jid, tags)}</td>
    </tr>"""


def _group_row(row: dict[str, Any]) -> str:
    # Communities never reach here — chats_index() splits them out to
    # their own tab (see _community_row) before building group_rows, so
    # there's no "is this a community" badge to render.
    jid = row["jid"]
    name = row.get("name") or jid
    tags = row.get("tags") or []
    topic = row.get("topic") or ""
    participant_count = row.get("participant_count", 0)
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
        <div class="row-name wa-flank wa-gap-s">
          {_avatar(row.get("avatar_url"), name)}
          <div class="meta wa-stack wa-gap-3xs">
            <span>{html.escape(name)}</span>
            <code>{html.escape(jid)}</code>
            {topic_div}
          </div>
        </div>
      </td>
      <td data-label="Members">{participant_count} members</td>
      <td data-label="Allowlist">{_allow_toggle(jid, bool(row.get("is_allowed")))}</td>
      <td data-label="Tags">{_metadata_form(jid, tags)}</td>
    </tr>"""


def _community_row(row: dict[str, Any]) -> str:
    # No members column, deliberately: a WhatsApp Community's own
    # "participants" list is just its admin(s)/owner, not real membership
    # (confirmed live — every community here had a single-digit count,
    # matching WhatsApp's own "size" field exactly) — showing it next to
    # "Community" reads as a bug, not a fact about the community. Actual
    # members live in the sub-groups underneath, which already appear as
    # their own ordinary rows on the Groups tab.
    jid = row["jid"]
    name = row.get("name") or jid
    tags = row.get("tags") or []
    topic = row.get("topic") or ""
    topic_text = html.escape(topic, quote=True) if topic else ""
    topic_div = (
        f'<div class="row-topic" title="{topic_text}">{topic_text}</div>' if topic_text else ""
    )
    base_search_blob = " ".join([name, jid, topic]).lower()
    search_blob = " ".join([base_search_blob, *(t.lower() for t in tags)]).strip()
    tags_json = html.escape(json.dumps([t.lower() for t in tags]), quote=True)
    return f"""
    <tr data-name="{html.escape(name.lower(), quote=True)}"
        data-search="{html.escape(search_blob, quote=True)}"
        data-base-search="{html.escape(base_search_blob, quote=True)}"
        data-tags="{tags_json}">
      <td>
        <div class="row-name wa-flank wa-gap-s">
          {_avatar(row.get("avatar_url"), name)}
          <div class="meta wa-stack wa-gap-3xs">
            <span>{html.escape(name)}</span>
            <code>{html.escape(jid)}</code>
            {topic_div}
          </div>
        </div>
      </td>
      <td data-label="Allowlist">{_allow_toggle(jid, bool(row.get("is_allowed")))}</td>
      <td data-label="Tags">{_metadata_form(jid, tags)}</td>
    </tr>"""


def _newsletter_row(row: dict[str, Any]) -> str:
    # Same shape as the original (pre-country-column) _direct_chat_row — a
    # channel's jid isn't a phone number, so there's no country to show.
    jid = row["jid"]
    name = row.get("name") or jid
    tags = row.get("tags") or []
    base_search_blob = " ".join([name, jid]).lower()
    search_blob = " ".join([base_search_blob, *(t.lower() for t in tags)]).strip()
    tags_json = html.escape(json.dumps([t.lower() for t in tags]), quote=True)
    return f"""
    <tr data-name="{html.escape(name.lower(), quote=True)}"
        data-search="{html.escape(search_blob, quote=True)}"
        data-base-search="{html.escape(base_search_blob, quote=True)}"
        data-tags="{tags_json}">
      <td>
        <div class="row-name wa-flank wa-gap-s">
          {_avatar(row.get("avatar_url"), name)}
          <div class="meta wa-stack wa-gap-3xs">
            <span>{html.escape(name)}</span>
            <code>{html.escape(jid)}</code>
          </div>
        </div>
      </td>
      <td data-label="Allowlist">{_allow_toggle(jid, bool(row.get("is_allowed")))}</td>
      <td data-label="Tags">{_metadata_form(jid, tags)}</td>
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
    """Filter chips above a table: a row of multi-select toggle buttons, each
    toggling whether rows carrying that tag are shown. Selected chips OR
    together (a row shows if it has *any* selected tag) — the usual semantics
    for multi-select within one facet, and it composes with the free-text
    search box via toggleTagFilter/applyTableFilter in the page script rather
    than replacing it."""
    if not tags:
        return ""
    chips = "".join(
        f"""<wa-button class="filter-chip" size="s" pill appearance="outlined" variant="neutral"
                       aria-pressed="false" data-tag="{html.escape(t.lower(), quote=True)}"
                       onclick="toggleTagFilter(this, '{table_id}')">{html.escape(t)}</wa-button>"""
        for t in tags
    )
    return (
        f'<div class="wa-cluster wa-gap-xs" id="{table_id}-filters" '
        f'data-search-input="{html.escape(search_input_id, quote=True)}">{chips}</div>'
    )


def _empty_row(colspan: int, message: str) -> str:
    return (
        f'<tr><td colspan="{colspan}"><p class="empty-state">{html.escape(message)}</p></td></tr>'
    )


def _groups_loaded_note(loaded: int, total: int) -> str:
    """ "42/159 groups loaded" — every real group jid ends "@g.us" (see
    admin/app.py's _classify), so the account's chat list is an
    independent, authoritative count of how many groups it's actually in,
    separate from whatever the bridge's dedicated groups endpoint returns.
    The two can disagree in a way worth surfacing rather than silently
    under-showing: WAHA's NOWEB engine (confirmed live) only returns full
    metadata for groups its own store has synced, which can lag well
    behind the chat list after a fresh pairing. Only rendered when there's
    an actual gap — once loaded catches up to total there's nothing to say.
    """
    if total == 0 or loaded >= total:
        return ""
    return f"""
    <p class="note">
      Loaded {loaded}/{total} groups. The rest have chat history but the
      bridge hasn't synced their metadata yet — this is a known gap in
      WAHA's NOWEB engine after a fresh pairing, not a botsapp bug. They
      should appear here as they get activity, or after a session restart.
    </p>"""


# Page script for the Chats page. A plain (non-f-string) constant so its braces
# need no escaping; browse_page() drops it into the page as a value.
_BROWSE_JS = """
  // Restore the active tab from the URL fragment — the allowlist/tags forms
  // redirect back to /chats#<panel-id> after a save so ticking a switch on
  // the Groups tab doesn't flip the page back to Chats. Set as an attribute
  // before <wa-tab-group> upgrades, so it is read on first render.
  (function () {
    var name = location.hash.slice(1);
    var group = document.querySelector("wa-tab-group");
    if (name && group && group.querySelector('wa-tab-panel[name="' + name + '"]')) {
      group.setAttribute("active", name);
    }
  })();

  // Redraws one row's tag pills (see _tag_chips) from an in-memory tags
  // array — called by addTags/removeTagAt below after they change that row's
  // tags, so the pills update the instant you hit Enter/comma or a pill's
  // remove button, rather than waiting on saveTags' round trip. Tag text uses
  // textContent (not the string-built markup _tag_chips renders server-side)
  // so a tag containing "<" or "&" can't be parsed as markup here.
  function renderTagChips(fieldId, tags) {
    var container = document.getElementById("tagchips-" + fieldId);
    if (!container) return;
    container.innerHTML = "";
    tags.forEach(function (t, i) {
      var tag = document.createElement("wa-tag");
      tag.setAttribute("size", "s");
      tag.setAttribute("with-remove", "");
      tag.dataset.idx = String(i);
      tag.textContent = t;
      container.appendChild(tag);
    });
  }

  // One delegated listener for every pill's remove button: pills are
  // re-rendered on each edit, so per-element listeners would be lost.
  document.addEventListener("wa-remove", function (event) {
    var tag = event.target.closest("wa-tag[data-idx]");
    var container = tag ? tag.closest("[data-field]") : null;
    if (container) removeTagAt(container.dataset.field, parseInt(tag.dataset.idx, 10));
  });

  // Keeps a row's data-search/data-tags in sync with its tags after an
  // in-place edit, so the search box and filter chips' "does this row match"
  // checks (applyTableFilter below) stay correct without a page reload.
  // Doesn't add a brand-new tag to the filter chip bar itself — that bar is
  // built once at page load from every row's tags (_distinct_tags); a tag
  // typed in since then is still searchable by text immediately, and shows up
  // as its own filter chip after the next reload.
  function updateRowTagData(fieldId, tags) {
    var hidden = document.getElementById("tags-" + fieldId);
    var row = hidden ? hidden.closest("tr") : null;
    if (!row) return;
    var tagsLower = tags.map(function (t) { return t.toLowerCase(); });
    row.dataset.tags = JSON.stringify(tagsLower);
    row.dataset.search = (row.dataset.baseSearch + " " + tagsLower.join(" ")).trim();
  }

  // Submits any of this page's small forms (the allow switch, the tags form)
  // in the background instead of navigating. A plain form submit would
  // navigate to /chats#<panel-id> and force a full page reload — which
  // re-fetches every chat/group's avatar from the WhatsApp bridge (see
  // admin/app.py chats_index) — turning a one-row DB write into a
  // multi-second wait. Whatever DOM update the caller needs (renderTagChips/
  // updateRowTagData for tags; the switch needs none) is expected to have
  // happened before this is called; fetch's own errors just get logged.
  function saveInBackground(form) {
    fetch(form.action, {method: "POST", body: new FormData(form)})
      .catch(function (err) { console.error("Failed to save:", err); });
  }

  function saveTags(fieldId) {
    var hidden = document.getElementById("tags-" + fieldId);
    if (!hidden) return;
    saveInBackground(hidden.form);
  }

  function splitTags(value) {
    return value.split(",").map(function (s) { return s.trim(); })
      .filter(function (s) { return s; });
  }

  // A tag pill's remove action: drop the tag at `idx` from that row's
  // comma-separated hidden tags field, by position — pills render in the
  // same order as the field's value.
  function removeTagAt(fieldId, idx) {
    var input = document.getElementById("tags-" + fieldId);
    if (!input) return;
    var tags = splitTags(input.value);
    tags.splice(idx, 1);
    input.value = tags.join(", ");
    renderTagChips(fieldId, tags);
    updateRowTagData(fieldId, tags);
    saveTags(fieldId);
  }

  // The "Add tag…" box's Enter/comma action: read the typed text (comma-
  // separated, so pasting "a, b" adds both), merge it into the row's hidden
  // tags field case-insensitively deduped against what's already there, and
  // clear the box.
  function addTags(fieldId) {
    var addInput = document.getElementById("addtag-" + fieldId);
    var hidden = document.getElementById("tags-" + fieldId);
    if (!addInput || !hidden) return;
    var newTags = splitTags(addInput.value);
    if (!newTags.length) return;
    var tags = splitTags(hidden.value);
    var seen = {};
    tags.forEach(function (t) { seen[t.toLowerCase()] = true; });
    newTags.forEach(function (t) {
      if (!seen[t.toLowerCase()]) {
        tags.push(t);
        seen[t.toLowerCase()] = true;
      }
    });
    hidden.value = tags.join(", ");
    addInput.value = "";
    renderTagChips(fieldId, tags);
    updateRowTagData(fieldId, tags);
    saveTags(fieldId);
  }

  // Live search + filter chips + column sort for the tables — pure
  // client-side (every row is already on the page; no round trip). Rows
  // without data-search are the "nothing here yet" empty-state row,
  // deliberately excluded from all three.

  // A filter chip's click (see _filter_chip_bar): toggle its selected state
  // (filled brand button vs outlined neutral one), then re-apply.
  function toggleTagFilter(chip, tableId) {
    var active = chip.getAttribute("aria-pressed") !== "true";
    chip.setAttribute("aria-pressed", active ? "true" : "false");
    chip.setAttribute("appearance", active ? "filled" : "outlined");
    chip.setAttribute("variant", active ? "brand" : "neutral");
    var bar = document.getElementById(tableId + "-filters");
    applyTableFilter(bar ? bar.dataset.searchInput : null, tableId);
  }

  // Combines the free-text search box with any selected filter chips: a row
  // must match the search term AND carry at least one selected tag (selected
  // tags themselves OR together — see _filter_chip_bar).
  function applyTableFilter(inputId, tableId) {
    var input = inputId ? document.getElementById(inputId) : null;
    var term = input ? String(input.value || "").trim().toLowerCase() : "";
    var activeTags = Array.prototype.slice.call(
      document.querySelectorAll("#" + tableId + '-filters .filter-chip[aria-pressed="true"]')
    ).map(function (chip) { return chip.dataset.tag; });
    document.querySelectorAll("#" + tableId + " tbody tr[data-search]").forEach(function (r) {
      var matchesSearch = !term || r.dataset.search.indexOf(term) !== -1;
      var matchesTags = true;
      if (activeTags.length) {
        var rowTags = JSON.parse(r.dataset.tags || "[]");
        matchesTags = activeTags.some(function (tag) { return rowTags.indexOf(tag) !== -1; });
      }
      r.style.display = matchesSearch && matchesTags ? "" : "none";
    });
  }

  function sortTable(th, tableId) {
    var table = document.getElementById(tableId);
    var tbody = table.querySelector("tbody");
    var rows = Array.prototype.slice.call(tbody.querySelectorAll("tr[data-search]"));
    if (!rows.length) return;
    var key = th.dataset.sortKey;
    var type = th.dataset.sortType;
    var dir = th.dataset.sortDir === "asc" ? "desc" : "asc";
    table.querySelectorAll("th[data-sort-key]").forEach(function (h) {
      delete h.dataset.sortDir;
      h.classList.remove("sort-asc", "sort-desc");
    });
    th.dataset.sortDir = dir;
    th.classList.add(dir === "asc" ? "sort-asc" : "sort-desc");
    rows.sort(function (a, b) {
      var av = a.dataset[key];
      var bv = b.dataset[key];
      if (type === "number") {
        av = parseFloat(av) || 0;
        bv = parseFloat(bv) || 0;
      }
      if (av < bv) return dir === "asc" ? -1 : 1;
      if (av > bv) return dir === "asc" ? 1 : -1;
      return 0;
    });
    rows.forEach(function (r) { tbody.appendChild(r); });
  }
"""


def _table_panel(
    name: str,
    columns: list[tuple[str, str | None, str | None]],
    rows_html: str,
    tags: list[str],
    search_label: str,
    note: str = "",
) -> str:
    """One tab's content: optional note, search box, filter chips and the
    table. ``name`` prefixes the element ids the page script looks up
    (``{name}-search``, ``{name}-table``, ``{name}-table-filters``);
    ``columns`` is (header, sort key, sort type) — a ``None`` key is
    unsortable."""
    table_id = f"{name}-table"
    search_id = f"{name}-search"
    heads = "".join(
        f'<th data-sort-key="{key}" data-sort-type="{kind}" '
        f"onclick=\"sortTable(this, '{table_id}')\">{html.escape(label)}</th>"
        if key
        else f"<th>{html.escape(label)}</th>"
        for label, key, kind in columns
    )
    return f"""
        {note}
        <wa-input class="table-toolbar" type="search" id="{search_id}"
                  placeholder="{html.escape(search_label, quote=True)}"
                  aria-label="{html.escape(search_label, quote=True)}"
                  oninput="applyTableFilter('{search_id}', '{table_id}')">
          <wa-icon slot="start" name="magnifying-glass"></wa-icon>
        </wa-input>
        {_filter_chip_bar(tags, table_id, search_id)}
        <table id="{table_id}" class="data-table">
          <thead><tr>{heads}</tr></thead>
          <tbody>{rows_html}</tbody>
        </table>"""


def browse_page(
    direct_rows: list[dict[str, Any]],
    group_rows: list[dict[str, Any]],
    community_rows: list[dict[str, Any]],
    newsletter_rows: list[dict[str, Any]],
    status_row: dict[str, Any] | None,
    bridge_name: str = "",
    group_count_loaded: int = 0,
    group_count_total: int = 0,
) -> str:
    direct_trs = "".join(_direct_chat_row(r) for r in direct_rows) or _empty_row(
        4, "No direct chats yet — give WhatsApp a moment to sync after logging in."
    )
    group_trs = "".join(_group_row(r) for r in group_rows) or _empty_row(
        4, "No groups found — the connected account isn't in any groups yet."
    )
    community_trs = "".join(_community_row(r) for r in community_rows) or _empty_row(
        3, "No communities found — the connected account isn't in any yet."
    )
    newsletter_trs = "".join(_newsletter_row(r) for r in newsletter_rows) or _empty_row(
        3, "No newsletter/channel subscriptions found."
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
        status_tab = '<wa-tab panel="status-panel">Status</wa-tab>'
        status_panel = f"""
      <wa-tab-panel name="status-panel">
        <div class="wa-stack wa-gap-m">
          <p class="note">
            WhatsApp's Status/Stories updates — a single feed, not a chat with a
            person or group. Most people leave this hidden.
          </p>
          {_allow_toggle(status_jid, bool(status_row.get("is_allowed")))}
          {_metadata_form(status_jid, status_tags)}
        </div>
      </wa-tab-panel>"""

    direct = _table_panel(
        "direct",
        [
            ("Chat", "name", "text"),
            ("Country", "country", "text"),
            ("Allowlist", None, None),
            ("Tags", None, None),
        ],
        direct_trs,
        _distinct_tags(direct_rows),
        "Search chats (name, tags)…",
    )
    groups = _table_panel(
        "groups",
        [
            ("Group", "name", "text"),
            ("Members", "members", "number"),
            ("Allowlist", None, None),
            ("Tags", None, None),
        ],
        group_trs,
        _distinct_tags(group_rows),
        "Search groups (name, topic, tags)…",
        note=_groups_loaded_note(group_count_loaded, group_count_total),
    )
    communities = _table_panel(
        "community",
        [("Community", "name", "text"), ("Allowlist", None, None), ("Tags", None, None)],
        community_trs,
        _distinct_tags(community_rows),
        "Search communities (name, topic, tags)…",
        note="""<p class="note">
          A WhatsApp Community groups several sub-groups under one umbrella —
          those sub-groups already appear on the Groups tab in their own
          right. No member count here: a community's own participant list is
          just its admin(s), not real membership.
        </p>""",
    )
    newsletters = _table_panel(
        "newsletter",
        [("Newsletter", "name", "text"), ("Allowlist", None, None), ("Tags", None, None)],
        newsletter_trs,
        _distinct_tags(newsletter_rows),
        "Search newsletters (name, tags)…",
    )

    body = f"""
    {_chat_header()}
    <p>
      Toggling "Visible to MCP" is the actual privacy boundary — Claude cannot see a
      chat, its messages, or its metadata until it's on here.
    </p>
    <wa-tab-group active="direct-panel">
      <wa-tab panel="direct-panel">Chats</wa-tab>
      <wa-tab panel="groups-panel">Groups</wa-tab>
      <wa-tab panel="community-panel">Communities</wa-tab>
      <wa-tab panel="newsletter-panel">Newsletters</wa-tab>
      {status_tab}

      <wa-tab-panel name="direct-panel"><div class="wa-stack wa-gap-m">{direct}</div></wa-tab-panel>
      <wa-tab-panel name="groups-panel"><div class="wa-stack wa-gap-m">{groups}</div></wa-tab-panel>
      <wa-tab-panel name="community-panel">
        <div class="wa-stack wa-gap-m">{communities}</div>
      </wa-tab-panel>
      <wa-tab-panel name="newsletter-panel">
        <div class="wa-stack wa-gap-m">{newsletters}</div>
      </wa-tab-panel>
      {status_panel}
    </wa-tab-group>
    <script>{_BROWSE_JS}</script>
    """
    return _page("Chats & Groups", body, bridge_name)


def _flash(notice: str, error: str) -> str:
    out = ""
    if notice:
        out += f'<wa-callout variant="success" role="status">{html.escape(notice)}</wa-callout>'
    if error:
        out += f'<wa-callout variant="danger" role="alert">{html.escape(error)}</wa-callout>'
    return out


def _role_select(role: str) -> str:
    options = "".join(
        f'<option value="{r}"{" selected" if r == role else ""}>{r}</option>'
        for r in ("admin", "member")
    )
    return f'<select name="role" aria-label="Role" onchange="this.form.submit()">{options}</select>'


def _user_row(user: dict[str, Any], current: str) -> str:
    email = user["email"]
    href = html.escape(quote(email, safe="@"), quote=True)
    label = html.escape(email)
    you = ' <wa-badge variant="neutral">you</wa-badge>' if email == current else ""
    role = "admin" if user["is_admin"] else "member"
    if email == current:
        controls = f'<td data-label="Role">{html.escape(role)}</td><td></td>'
    else:
        controls = f"""
      <td data-label="Role">
        <form method="post" action="{p("/users")}/{href}/role">{_role_select(role)}</form>
      </td>
      <td>
        <form method="post" action="{p("/users")}/{href}/remove"
              onsubmit="return confirm('Remove this user? They lose all access.');">
          <wa-button type="submit" appearance="plain" variant="danger" title="Remove">
            <wa-icon name="trash" label="Remove"></wa-icon>
          </wa-button>
        </form>
      </td>"""
    return f'<tr><td data-label="Email">{label}{you}</td>{controls}</tr>'


def users_page(
    users: list[dict[str, Any]],
    current: str,
    managed: bool,
    notice: str = "",
    error: str = "",
    bridge_name: str = "",
) -> str:
    """Who is a botsapp user and their role. With ``managed`` set, invitations,
    removals and role changes also go to WorkOS (docs/decisions/0026)."""
    flash = _flash(notice, error)
    if not managed:
        flash += (
            '<wa-callout variant="warning">WorkOS is not connected: changes here only affect '
            "this server's own list, and no invitation email is sent.</wa-callout>"
        )
    rows = (
        "".join(_user_row(u, current) for u in users)
        if users
        else '<tr><td colspan="3" class="empty-state">No users yet.</td></tr>'
    )
    body = f"""
    {flash}
    <p class="note">
      Only these people can use botsapp. <b>admin</b> can manage users; <b>member</b>
      can use the accounts they own or were granted. Which chats someone sees is
      set per account, not here.
    </p>
    <wa-card class="account-card">
      <form method="post" action="{p("/users/invite")}" class="wa-stack wa-gap-m">
        <wa-input type="email" name="email" label="Email to invite" required></wa-input>
        <div class="wa-cluster wa-gap-s">
          <select name="role" aria-label="Role">
            <option value="member">member</option>
            <option value="admin">admin</option>
          </select>
          <wa-button type="submit" variant="brand">Invite</wa-button>
        </div>
      </form>
    </wa-card>
    <table class="data-table account-card">
      <thead><tr><th>Email</th><th>Role</th><th></th></tr></thead>
      <tbody>{rows}</tbody>
    </table>"""
    return _page("Users", body, bridge_name)


def _grant_row(account_id: str, email: str) -> str:
    aid = html.escape(quote(account_id, safe=""), quote=True)
    choose_chats = _select_account_form(
        account_id, label="Choose chats", next_url=p(f"/chats?viewer={quote(email, safe='@')}")
    )
    return f"""
      <div class="wa-cluster wa-gap-s">
        <span>{html.escape(email)}</span>
        {choose_chats}
        <form method="post" action="{p("/accounts")}/{aid}/revoke" class="inline-form"
              onsubmit="return confirm('Revoke access? They stop seeing this account at once.');">
          <input type="hidden" name="email" value="{html.escape(email, quote=True)}">
          <wa-button type="submit" appearance="plain" variant="danger" size="s" title="Revoke">
            <wa-icon name="xmark" label="Revoke"></wa-icon>
          </wa-button>
        </form>
      </div>"""


def _account_card(row: dict[str, Any], current: str) -> str:
    aid = html.escape(quote(row["id"], safe=""), quote=True)
    label = html.escape(row["label"])
    badge = '<wa-badge variant="neutral">selected</wa-badge>' if row["id"] == current else ""
    pairing = _pairing_panel(row["id"], row["session"])
    grants = "".join(_grant_row(row["id"], e) for e in row["granted"])
    grants = (
        f'<div class="wa-stack wa-gap-xs">{grants}</div>'
        if grants
        else '<p class="note">Not shared with anyone.</p>'
    )
    options = "".join(
        f'<option value="{html.escape(u, quote=True)}">{html.escape(u)}</option>'
        for u in row["grantable"]
    )
    grant_form = (
        f"""
      <form method="post" action="{p("/accounts")}/{aid}/grant" class="wa-cluster wa-gap-s">
        <select name="email" aria-label="Share with" required>{options}</select>
        <wa-button type="submit" appearance="outlined">Share</wa-button>
      </form>"""
        if options
        else ""
    )
    key_block = ""
    if row.get("manage_key"):
        if row.get("key_id"):
            state_text = (
                "read-only, as intended"
                if not row.get("key_drift")
                else "<b>differs from intended</b>: " + html.escape(row["key_drift"])
            )
            repair = (
                f"""
        <form method="post" action="{p("/accounts")}/{aid}/key/repair" class="inline-form">
          <wa-button type="submit" appearance="outlined" size="s" variant="warning">
            Repair</wa-button></form>"""
                if row.get("key_drift")
                else ""
            )
            buttons = f"""
        <div class="wa-cluster wa-gap-s">
          <form method="post" action="{p("/accounts")}/{aid}/key/verify" class="inline-form">
            <wa-button type="submit" appearance="outlined" size="s">Verify</wa-button></form>
          {repair}
          <form method="post" action="{p("/accounts")}/{aid}/key/rotate" class="inline-form"
                onsubmit="return confirm('Replace this account\\'s access key?');">
            <wa-button type="submit" appearance="outlined" size="s">Rotate</wa-button></form>
        </div>"""
        elif row.get("can_store_key"):
            state_text = "none: the MCP server is using the shared admin key for this account"
            buttons = f"""
        <form method="post" action="{p("/accounts")}/{aid}/key/create" class="inline-form">
          <wa-button type="submit" appearance="outlined" size="s">
            Create access key</wa-button></form>"""
        else:
            state_text = "none, and BOTSAPP_SECRET_KEY isn't set so one can't be stored"
            buttons = ""
        key_block = f"""
      <p class="note">WAHA access key: {state_text}</p>{buttons}"""
    delete = (
        f"""
      <form method="post" action="{p("/accounts")}/{aid}/delete"
            onsubmit="return confirm('Delete this account and its WhatsApp session?');">
        <wa-button type="submit" appearance="outlined" variant="danger">
          Delete account</wa-button>
      </form>"""
        if row["deletable"]
        else ""
    )
    return f"""
    <wa-card class="account-card">
      <div slot="header" class="wa-cluster wa-gap-s"><strong>{label}</strong>{badge}</div>
      <div class="wa-stack wa-gap-m">
        <p class="note">id <code>{html.escape(row["id"])}</code> · owner
          {html.escape(row["owner"])}</p>
        {pairing}
        <div>{_select_account_form(row["id"], label="Use this account for Chats")}</div>
        {key_block}
        <div class="wa-stack wa-gap-xs"><b>Shared with</b>{grants}</div>
        {grant_form}
        {delete}
      </div>
    </wa-card>"""


def session_page(
    rows: list[dict[str, Any]],
    shared: list[tuple[str, str, str]],
    users: list[str],
    is_admin: bool,
    current: str,
    notice: str = "",
    error: str = "",
    bridge_name: str = "",
) -> str:
    """Every WhatsApp account you own (all of them, for an admin) or were
    granted, with live Connect/QR/Disconnect controls inline on each one
    (0029: the old standalone Session page showed this for only whichever
    account happened to be selected; folded in here, per account, instead).
    ``current`` is still used — it's which account the Chats page acts on."""
    flash = _flash(notice, error)
    create = ""
    if is_admin:
        owners = "".join(
            f'<option value="{html.escape(u, quote=True)}">{html.escape(u)}</option>' for u in users
        )
        create = f"""
    <wa-card class="account-card">
      <form method="post" action="{p("/accounts/create")}" class="wa-stack wa-gap-m">
        <strong>New account</strong>
        <div class="wa-cluster wa-gap-s">
          <wa-input name="id" label="id" placeholder="e.g. sam" required></wa-input>
          <wa-input name="label" label="label"></wa-input>
        </div>
        <div class="wa-cluster wa-gap-s">
          <select name="owner" aria-label="Owner" required>{owners}</select>
          <wa-button type="submit" variant="brand">Create</wa-button>
        </div>
        <p class="note">Creates the account and an empty WhatsApp session, ready for the
          owner to pair below.</p>
      </form>
    </wa-card>"""
    cards = "".join(_account_card(r, current) for r in rows) or (
        "<p class='note'>You don't own any account yet.</p>"
    )
    shared_list = ""
    if shared:
        items = "".join(
            f'<div class="wa-cluster wa-gap-xs">{_select_account_form(i, label=label)}'
            f"<span>shared by {html.escape(owner)}</span></div>"
            for i, label, owner in shared
        )
        shared_list = f'<h2>Shared with you</h2><div class="wa-stack wa-gap-xs">{items}</div>'
    any_qr_pending = any(
        r["session"].get("qr_code") and not r["session"].get("logged_in")
        for r in rows
        if not r["session"].get("errors")
    )
    # Belt-and-suspenders fallback: a background tab's JS poll timers can get
    # throttled or paused outright, silently leaving a dead QR on screen. A
    # page-level meta-refresh is safe now that this page never triggers
    # connect on its own — worst case it just re-renders current state.
    refresh = '<meta http-equiv="refresh" content="15">' if any_qr_pending else ""
    body = f"{flash}{refresh}{create}{cards}{shared_list}"
    return _page("Session", body, bridge_name)
