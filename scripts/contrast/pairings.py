"""The catalog of real foreground-on-background pairings that occur in the
rendered UI, derived from grepping the actual CSS rules (not guessed).

Each ``Pairing.source`` names the file:line(s) the pairing was read from so
this list stays auditable against the code instead of drifting into a list
someone remembers. Normal text uses the WCAG 4.5:1 threshold; large text
(>=18.66px bold or >=24px, e.g. sidebar titles, header h1) and UI component
boundaries (focus rings, input borders) use 3:1.
"""
from __future__ import annotations

from .color_utils import Pairing

NORMAL = 4.5
LARGE = 3.0

PAIRINGS: list[Pairing] = [
    # ---- Body / page ------------------------------------------------
    Pairing("body text", "color-fg", "color-bg", NORMAL, "styles.css:234-243 body{background:--color-bg;color:--color-fg}"),
    Pairing("header title", "color-fg", "color-bg-page", NORMAL, "styles.css:247-251 .header{background:--color-bg-page}, h1 inherits --color-fg"),

    # ---- Session sidebar (client/css/session-sidebar.css) -----------
    # Panel background is --color-bg-elevated (line 71); every row control
    # is `background: transparent`, i.e. it sits directly on the panel bg.
    Pairing("sidebar toggle icon", "color-fg-muted", "color-bg-elevated", NORMAL, "session-sidebar.css:33,71"),
    Pairing("sidebar pin icon", "color-fg-muted", "color-bg-elevated", NORMAL, "session-sidebar.css:138,71"),
    Pairing("sidebar close icon", "color-fg-muted", "color-bg-elevated", NORMAL, "session-sidebar.css:195,71"),
    Pairing("sidebar empty state", "color-fg-subtle", "color-bg-elevated", NORMAL, "session-sidebar.css:216,71"),
    Pairing("sidebar row name", "color-fg", "color-bg-elevated", NORMAL, "session-sidebar.css:252,71"),
    Pairing("sidebar row badge", "color-fg-faint", "color-bg-elevated", NORMAL, "session-sidebar.css:265,71"),
    Pairing("sidebar mark-unread icon", "color-fg-faint", "color-bg-elevated", NORMAL, "session-sidebar.css:360,71"),
    Pairing("sidebar delete icon", "color-fg-faint", "color-bg-elevated", NORMAL, "session-sidebar.css:401,71"),
    Pairing("sidebar title", "color-accent", "color-bg-elevated", LARGE, "session-sidebar.css:176,71 .session-sidebar-title bold"),

    # ---- Home bar / launchpad (client/css/home-bar.css) -------------
    Pairing("home bar button label", "color-fg-muted", "color-bg-elevated", NORMAL, "home-bar.css:113,147"),
    Pairing("home bar version label", "color-fg-faint", "color-bg-elevated", NORMAL, "home-bar.css:113,212"),

    # ---- Toast (client/css/toast.css) --------------------------------
    Pairing("toast body text", "color-fg", "color-bg-card", NORMAL, "toast.css:38 bg:--color-bg-card"),
    Pairing("toast muted text", "color-fg-muted", "color-bg-card", NORMAL, "toast.css:38,75"),

    # ---- Config editor / drawer (modal, bg-elevated card) -----------
    Pairing("config editor body text", "color-fg", "color-bg", NORMAL, "config-editor.css:97 on modal bg:--color-bg (config-editor-modal.css:30)"),
    Pairing("config editor muted text", "color-fg-muted", "color-bg", NORMAL, "config-editor.css:62,86,123 on modal bg"),
    Pairing("config editor subtle text", "color-fg-subtle", "color-bg", NORMAL, "config-editor.css:104,183 on modal bg"),
    Pairing("config drawer muted text", "color-fg-muted", "color-bg-elevated", NORMAL, "config-drawer.css:106 (drawer surface bg-elevated)"),

    # ---- Server status panel (client/css/server-status.css) ---------
    Pairing("server status muted label", "color-fg-muted", "color-bg-card", NORMAL, "server-status.css:69,118,199 (card surface)"),
    Pairing("server status body text", "color-fg", "color-bg-card", NORMAL, "server-status.css:78,124,143,170"),
    Pairing("server status faint text", "color-fg-faint", "color-bg-card", NORMAL, "server-status.css:176"),
    Pairing("server status subtle text", "color-fg-subtle", "color-bg-card", NORMAL, "server-status.css:184"),

    # ---- Slash command chips (client/css/slash-command-chips.css) ---
    Pairing("slash command chip hint", "color-fg-faint", "color-bg-elevated", NORMAL, "slash-command-chips.css:32,83,96,138"),

    # ---- Terminal tools bar ------------------------------------------
    Pairing("terminal tools muted icon", "color-fg-muted", "color-bg-elevated", NORMAL, "terminal-tools.css:53,239,329"),
    Pairing("terminal tools body text", "color-fg", "color-bg-elevated", NORMAL, "terminal-tools.css:229,365"),

    # ---- Badges (styles.css:97-102, 2341-2342) -----------------------
    Pairing("badge owned", "color-badge-owned-fg", "color-badge-owned-bg", NORMAL, "styles.css:97-98,2341"),
    Pairing("badge tmux", "color-badge-tmux-fg", "color-badge-tmux-bg", NORMAL, "styles.css:99-100,2341"),
    Pairing("badge external", "color-badge-external-fg", "color-badge-external-bg", NORMAL, "styles.css:101-102,2342"),

    # ---- Status text (success/warning/danger/info on page bg) -------
    # Normal-text threshold: grep confirms these tokens paint regular-size
    # body text (error/warning messages in config-editor*.css, server-
    # status.css, session-sidebar.css), not only small icon dots.
    Pairing("success text", "color-success", "color-bg", NORMAL, "styles.css:71, used as text color e.g. server-status.css"),
    Pairing("warning text", "color-warning", "color-bg", NORMAL, "styles.css:72, config-editor.css:389, session-sidebar.css:380"),
    Pairing("danger text", "color-danger", "color-bg", NORMAL, "styles.css:73, config-editor.css:168, session-sidebar.css:409"),
    Pairing("info text", "color-info", "color-bg", NORMAL, "styles.css:74, styles.css:930"),

    # ---- Accent as text/icon on page + elevated surfaces -------------
    # LARGE (3:1) threshold: a grep of every `color: var(--color-accent)`
    # site (config-editor*.css, session-sidebar.css, home-bar.css,
    # header-menu.css) shows it painting icons, short bold labels
    # (.session-sidebar-title), hover states and borders - UI-component
    # territory, not paragraph body copy - so the UI-component-boundary
    # threshold applies rather than the stricter normal-text one.
    Pairing("accent link on bg", "color-accent", "color-bg", LARGE, "styles.css:53 --color-accent used as link/icon color"),
    Pairing("accent link on elevated", "color-accent", "color-bg-elevated", LARGE, "styles.css:53, many .row:hover states land on elevated surfaces"),
    Pairing("on-accent button text", "color-on-accent", "color-accent", NORMAL, "styles.css:65 text painted on a solid accent fill (primary buttons)"),

    # ---- Home screen ownership stripe (styles.css .running-session-row)
    # Row background is --color-accent-bg-soft, a low-alpha rgba() tint,
    # composited over --color-bg (the page behind it) rather than read as
    # a flat swatch. The stripe conveys ownership (owned=tmux/internal,
    # external=adopted) and is now driven by the same tokens as the badge
    # pill in the same row so the two always agree.
    Pairing("ownership stripe: owned", "color-badge-tmux-fg", "color-accent-bg-soft", LARGE,
            "styles.css .running-session-row.owned border-left; row bg composited over --color-bg",
            bg_under_token="color-bg"),
    Pairing("ownership stripe: external", "color-badge-external-fg", "color-accent-bg-soft", LARGE,
            "styles.css .running-session-row.external border-left; row bg composited over --color-bg",
            bg_under_token="color-bg"),

    # ---- Help disclosure README link (styles.css .adopt-disclosure-body a)
    Pairing("help link (visited/hover)", "color-accent-strong", "color-bg-elevated", NORMAL,
            "styles.css .adopt-disclosure-body a:visited / :hover on --color-bg-elevated"),
    Pairing("help link focus ring", "color-accent", "color-bg-elevated", LARGE,
            "styles.css .adopt-disclosure-body a:focus-visible outline on --color-bg-elevated"),

    # ---- Focus indicators (3:1 UI-component-boundary threshold) -----
    # session-sidebar.css:373 `.mark-unread-toggle:focus-visible { box-
    # shadow: 0 0 0 2px var(--color-accent-border) }` and the matrix
    # theme's own input:focus border (theme.css:44-49) both use the
    # accent against the field/row background - this is the one boundary
    # that is NOT decorative: WCAG 1.4.11 explicitly covers focus
    # indicators. A plain 1px divider between two already-distinct
    # regions (e.g. the sidebar panel's own border) is decorative and
    # deliberately excluded here.
    Pairing("focus indicator", "color-accent", "color-bg-elevated", LARGE, "session-sidebar.css:373 focus-visible box-shadow accent on row/panel bg"),
    Pairing("input focus border", "color-accent", "color-bg", LARGE, "matrix theme.css:44-49 focus border must read against field bg, generalised fleet-wide"),
]
