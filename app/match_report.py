"""Anatomy of a Dota 2 match: an explainer report for one OpenDota match response.

Run:  streamlit run app/match_report.py
Reads sample responses from data/samples/ (produced by scripts/explore_opendota.py).
"""

import json
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "samples"

MEDALS = {1: "Herald", 2: "Guardian", 3: "Crusader", 4: "Archon", 5: "Legend", 6: "Ancient", 7: "Divine", 8: "Immortal"}
POSITIONS = {1: "Carry", 2: "Mid", 3: "Offlane", 4: "Soft support", 5: "Hard support"}
# Valve's gold-change reason codes (DOTA_ModifyGold_*); rarer codes fold into "Other".
GOLD_REASONS = {
    0: "Other", 1: "Lost on death", 2: "Buyback", 6: "Selling items", 11: "Buildings", 12: "Hero kills",
    13: "Lane creeps", 14: "Neutral creeps", 15: "Roshan", 16: "Courier kills", 17: "Bounty runes",
}
NEGATIVE_REASONS = {1, 2}

# Reference palette, categorical slots 1-2 (validated adjacent pair), plus chart chrome.
THEMES = {
    "light": {"radiant": "#2a78d6", "dire": "#eb6834", "muted": "#898781", "grid": "#e1e0d9", "band": "rgba(11,11,11,0.06)"},
    "dark": {"radiant": "#3987e5", "dire": "#d95926", "muted": "#898781", "grid": "#2c2c2a", "band": "rgba(255,255,255,0.07)"},
}


# ---------------------------------------------------------------- data loading

def _load(name):
    return json.loads((SAMPLES / name).read_text(encoding="utf-8"))


@st.cache_data
def load_reference():
    heroes = _load("heroes.json")
    items = _load("constants_items.json")
    return {
        "hero_by_id": {h["id"]: h["localized_name"] for h in heroes},
        "hero_by_key": {h["name"]: h["localized_name"] for h in heroes},
        "item_by_id": {v["id"]: v.get("dname") or k for k, v in items.items() if "id" in v},
        "item_by_key": {k: v.get("dname") or k for k, v in items.items()},
        "item_cost": {k: v.get("cost") or 0 for k, v in items.items()},
        "patch_by_id": {p["id"]: p["name"] for p in _load("constants_patch.json")},
        "regions": _load("constants_region.json"),
    }


@st.cache_data
def available_matches():
    """Finished matches among the saved samples, keyed by a readable label."""
    out = {}
    for path in sorted(SAMPLES.glob("match_*.json")):
        m = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(m, dict) and m.get("players") and m.get("duration"):
            kind = "parsed" if m.get("od_data", {}).get("has_parsed") else "unparsed"
            out[f"{m['match_id']} ({kind}, lobby {m.get('lobby_type')})"] = (m.get("lobby_type") != 7, path.name)
    # Ranked matches first: they're the representative case.
    return {label: name for label, (_, name) in sorted(out.items(), key=lambda kv: kv[1])}


def medal(rank_tier):
    if not rank_tier:
        return "Hidden"
    return f"{MEDALS.get(rank_tier // 10, '?')} {rank_tier % 10}"


def mmss(seconds):
    sign = "-" if seconds < 0 else ""
    seconds = abs(int(seconds))
    return f"{sign}{seconds // 60}:{seconds % 60:02d}"


def build_players(match, ref):
    rows = []
    for p in sorted(match["players"], key=lambda p: p["player_slot"]):
        pos = p.get("position_est") or p.get("lane_role")
        rows.append({
            "slot": p["player_slot"],
            "team": "Radiant" if p["isRadiant"] else "Dire",
            "hero": ref["hero_by_id"].get(p["hero_id"], f"Hero {p['hero_id']}"),
            "position": pos,
            "role": POSITIONS.get(pos, "Unknown"),
            "rank": medal(p.get("rank_tier")),
            "kills": p["kills"], "deaths": p["deaths"], "assists": p["assists"],
            "net_worth": p.get("net_worth"), "last_hits": p["last_hits"], "denies": p["denies"],
            "gpm": p["gold_per_min"], "xpm": p["xp_per_min"], "hero_damage": p.get("hero_damage"),
            "lh_10": p["lh_t"][10] if len(p.get("lh_t") or []) > 10 else None,
            "nw_10": p["networth_t"][10] if len(p.get("networth_t") or []) > 10 else None,
            "raw": p,
        })
    return pd.DataFrame(rows)


def team_of_slot(slot):
    return "Radiant" if slot < 128 else "Dire"


def build_events(match, players):
    """Key match events as rows, each tagged with the team that benefited."""
    hero_of_slot = dict(zip(players["slot"], players["hero"]))
    rows = []
    for o in match.get("objectives") or []:
        t, typ = o["time"] / 60, o["type"]
        if typ == "building_kill":
            lost_by = "Radiant" if "goodguys" in o["key"] else "Dire"
            building = o["key"].replace("npc_dota_goodguys_", "").replace("npc_dota_badguys_", "")
            kind = "Barracks" if "rax" in building else "Tower" if "tower" in building else "Building"
            rows.append({"minute": t, "lane": "Buildings destroyed", "team": "Dire" if lost_by == "Radiant" else "Radiant",
                         "label": f"{lost_by} lost {kind.lower()} ({building.replace('_', ' ')})"})
        elif typ == "CHAT_MESSAGE_ROSHAN_KILL":
            team = "Radiant" if o.get("team") == 2 else "Dire"
            rows.append({"minute": t, "lane": "Roshan", "team": team, "label": f"{team} killed Roshan"})
        elif typ == "CHAT_MESSAGE_FIRSTBLOOD":
            slot = o.get("player_slot", 0)
            rows.append({"minute": t, "lane": "First blood", "team": team_of_slot(slot),
                         "label": f"First blood by {hero_of_slot.get(slot, '?')}"})
        elif typ == "CHAT_MESSAGE_COURIER_LOST":
            lost_by = "Radiant" if o.get("team") == 2 else "Dire"
            rows.append({"minute": t, "lane": "Couriers killed", "team": "Dire" if lost_by == "Radiant" else "Radiant",
                         "label": f"{lost_by} courier killed"})
    return pd.DataFrame(rows, columns=["minute", "lane", "team", "label"])


# ---------------------------------------------------------------- chart helpers

def base_layout(fig, colors, height, **kwargs):
    fig.update_layout(
        height=height, margin=dict(l=8, r=8, t=8, b=8), hovermode="closest",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0, title=None),
        font=dict(family='system-ui, -apple-system, "Segoe UI", sans-serif'), **kwargs,
    )
    fig.update_xaxes(gridcolor=colors["grid"], zeroline=False, showline=False)
    fig.update_yaxes(gridcolor=colors["grid"], zeroline=False, showline=False)
    return fig


def with_alpha(hex_color, alpha):
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return f"rgba({r},{g},{b},{alpha})"


def show(fig):
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})


def stat(col, label, value, help_text=None):
    col.metric(label, value, help=help_text)


def source_note(fields):
    st.caption("API fields used: " + ", ".join(f"`{f}`" for f in fields))


# ---------------------------------------------------------------- sections

def section_overview(match, players, ref):
    st.header("1 · The match at a glance")
    st.write(
        "Dota 2 is played by two teams of five: **Radiant** and **Dire**. Each player controls one *hero*. "
        "Heroes earn **gold** (to buy items) and **experience** (to level up) by killing enemy units and heroes. "
        "The team that destroys the other team's base (the *Ancient*) wins."
    )
    winner = "Radiant" if match["radiant_win"] else "Dire"
    known = [p["rank_tier"] for p in match["players"] if p.get("rank_tier")]
    avg_rank = round(sum(known) / len(known)) if known else None
    c = st.columns(5)
    stat(c[0], "Winner", winner)
    stat(c[1], "Duration", mmss(match["duration"]))
    stat(c[2], "Kills (Radiant–Dire)", f"{match['radiant_score']}–{match['dire_score']}")
    stat(c[3], "Average rank", medal(avg_rank), f"Mean of {len(known)} visible player ranks")
    stat(c[4], "Patch", ref["patch_by_id"].get(match.get("patch"), "?"),
         "Dota changes with each patch: heroes and items get rebalanced, so stats are only comparable within a patch")
    lobby = {7: "Ranked", 0: "Unranked"}.get(match.get("lobby_type"), f"lobby {match.get('lobby_type')}")
    mode = {22: "All Pick", 23: "Turbo"}.get(match.get("game_mode"), f"mode {match.get('game_mode')}")
    st.caption(f"Match {match['match_id']} · {lobby} {mode} · server region: "
               f"{ref['regions'].get(str(match.get('region')), '?').title()} · "
               f"started {pd.to_datetime(match['start_time'], unit='s'):%Y-%m-%d %H:%M} UTC")
    source_note(["radiant_win", "duration", "radiant_score", "dire_score", "players[].rank_tier", "patch", "lobby_type", "game_mode"])


def section_lineups(players):
    st.header("2 · Who played")
    st.write(
        "Every hero fills a **position** from 1 to 5. Positions 1–3 are *cores*: they take most of the gold and are "
        "expected to deal the damage. Positions 4–5 are *supports*: they give up gold to protect cores and provide vision. "
        "Comparing a support's gold with a carry's would be misleading, which is why the coach will always compare within a position."
    )
    max_nw = int(players["net_worth"].max())
    for team in ("Radiant", "Dire"):
        df = players[players["team"] == team].sort_values("position")
        st.subheader(f"{team}{'  🏆' if df['raw'].iloc[0]['win'] else ''}")
        st.dataframe(
            df.assign(kda=df["kills"].astype(str) + " / " + df["deaths"].astype(str) + " / " + df["assists"].astype(str))[
                ["position", "role", "hero", "rank", "kda", "net_worth", "last_hits", "gpm", "hero_damage"]],
            hide_index=True, width="stretch",
            column_config={
                "position": st.column_config.NumberColumn("Pos", width="small"),
                "role": "Role", "hero": "Hero", "rank": "Rank",
                "kda": st.column_config.TextColumn("K / D / A", help="Kills / Deaths / Assists"),
                "net_worth": st.column_config.ProgressColumn("Net worth", format="%d", min_value=0, max_value=max_nw,
                                                             help="Gold + value of items at the end of the game"),
                "last_hits": st.column_config.NumberColumn("Last hits", help="Enemy creeps killed with the final blow (the main gold source)"),
                "gpm": st.column_config.NumberColumn("Gold/min"),
                "hero_damage": st.column_config.NumberColumn("Hero damage", format="%d"),
            },
        )
    source_note(["players[].position_est", "hero_id", "kills", "deaths", "assists", "net_worth", "last_hits", "gold_per_min", "hero_damage"])


def section_flow(match, events, colors):
    st.header("3 · How the game swung")
    if not match.get("radiant_gold_adv"):
        st.info("This match was not parsed, so the minute-by-minute team advantage is not available.")
        return
    measure = st.radio("Advantage measured in", ["Gold", "Experience"], horizontal=True, label_visibility="collapsed")
    series = match["radiant_gold_adv"] if measure == "Gold" else match["radiant_xp_adv"]
    df = pd.DataFrame({"minute": range(len(series)), "adv": series})
    st.write(
        f"The line shows how far ahead one team was in total **{measure.lower()}**, minute by minute. "
        "Above zero (blue) Radiant leads, below zero (orange) Dire leads. Gray bands are **teamfights**. "
        "The strip underneath marks key events, colored by the team that *benefited*."
    )

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.72, 0.28], vertical_spacing=0.04)
    fig.add_trace(go.Scatter(x=df["minute"], y=df["adv"].clip(lower=0), fill="tozeroy", mode="none",
                             fillcolor=with_alpha(colors["radiant"], 0.35), hoverinfo="skip", showlegend=False), 1, 1)
    fig.add_trace(go.Scatter(x=df["minute"], y=df["adv"].clip(upper=0), fill="tozeroy", mode="none",
                             fillcolor=with_alpha(colors["dire"], 0.35), hoverinfo="skip", showlegend=False), 1, 1)
    leader = ["Radiant ahead by" if v >= 0 else "Dire ahead by" for v in df["adv"]]
    fig.add_trace(go.Scatter(x=df["minute"], y=df["adv"], mode="lines", line=dict(color=colors["muted"], width=2),
                             customdata=list(zip(leader, df["adv"].abs())), showlegend=False,
                             hovertemplate="Minute %{x}<br>%{customdata[0]} %{customdata[1]:,}<extra></extra>"), 1, 1)
    for tf in match.get("teamfights") or []:
        fig.add_vrect(x0=tf["start"] / 60, x1=tf["end"] / 60, fillcolor=colors["band"], line_width=0, row="all", col=1)

    # Direct-label the biggest lead of each side.
    for idx, team in ((df["adv"].idxmax(), "Radiant"), (df["adv"].idxmin(), "Dire")):
        v = df["adv"][idx]
        if (v > 0) == (team == "Radiant") and v != 0:
            fig.add_annotation(x=idx, y=v, text=f"Biggest {team} lead: {abs(v):,}", showarrow=True, arrowhead=0,
                               ax=0, ay=-24 if v > 0 else 24, font=dict(size=12), row=1, col=1)

    lanes = ["First blood", "Buildings destroyed", "Roshan", "Couriers killed"]
    for team in ("Radiant", "Dire"):
        ev = events[events["team"] == team]
        fig.add_trace(go.Scatter(x=ev["minute"], y=ev["lane"], mode="markers", name=f"{team} benefited",
                                 marker=dict(color=colors[team.lower()], size=10, line=dict(width=2, color="rgba(0,0,0,0)")),
                                 text=ev["label"], hovertemplate="%{text}<br>at %{x:.1f} min<extra></extra>"), 2, 1)
    fig.update_yaxes(title=f"{measure} lead", row=1, col=1)
    fig.update_yaxes(categoryorder="array", categoryarray=lanes[::-1], row=2, col=1)
    fig.update_xaxes(title="Game minute", row=2, col=1)
    show(base_layout(fig, colors, 520))

    with st.expander("Show the data"):
        st.dataframe(events.assign(time=events["minute"].mul(60).map(mmss))[["time", "lane", "team", "label"]]
                     .rename(columns={"lane": "event type", "team": "benefited"}), hide_index=True, width="stretch")
    source_note(["radiant_gold_adv", "radiant_xp_adv", "teamfights[].start/end", "objectives[]"])


def section_laning(players, colors):
    st.header("4 · The laning phase: first 10 minutes")
    if players["lh_10"].isna().all():
        st.info("Per-minute data needs a parsed match.")
        return
    st.write(
        "The first ten minutes decide a lot. Each hero is paired against an enemy in the same position, and "
        "**last hits at minute 10** is the classic measure of who won the lane economically. "
        "Bars are grouped by position, so you're comparing like with like."
    )
    df = players.dropna(subset=["position"]).sort_values("position")
    df["pos_label"] = df["position"].map(lambda p: f"Pos {p}<br>{POSITIONS.get(p, '')}")
    fig = go.Figure()
    for team in ("Radiant", "Dire"):
        d = df[df["team"] == team]
        fig.add_trace(go.Bar(x=d["pos_label"], y=d["lh_10"], name=team, marker_color=colors[team.lower()],
                             marker_cornerradius=4, text=d["hero"], textposition="outside",
                             customdata=d["nw_10"], hovertemplate="%{text}<br>Last hits @10: %{y}<br>Net worth @10: %{customdata:,}<extra>" + team + "</extra>"))
    fig.update_layout(barmode="group", bargap=0.3, bargroupgap=0.08)
    fig.update_yaxes(title="Last hits at 10:00")
    show(base_layout(fig, colors, 380))
    with st.expander("Show the data"):
        st.dataframe(df[["team", "position", "role", "hero", "lh_10", "nw_10"]]
                     .rename(columns={"lh_10": "last hits @10", "nw_10": "net worth @10"}), hide_index=True, width="stretch")
    source_note(["players[].lh_t[10]", "players[].networth_t[10]", "position_est"])


def section_player(players, ref, colors):
    st.header("5 · One player's game, step by step")
    if not players["raw"].iloc[0].get("gold_t"):
        st.info("Detailed player timelines need a parsed match.")
        return
    labels = [f"{r.hero} ({r.team}, pos {r.position})" for r in players.itertuples()]
    choice = st.selectbox("Pick a player", labels, index=int(players["position"].eq(1).idxmax()))
    row = players.iloc[labels.index(choice)]
    p, team_color = row["raw"], colors[row["team"].lower()]
    duration = p["duration"]

    c = st.columns(4)
    stat(c[0], "K / D / A", f"{p['kills']} / {p['deaths']} / {p['assists']}")
    stat(c[1], "Last hits @10", row["lh_10"])
    dead = p.get("life_state_dead") or 0
    stat(c[2], "Time spent dead", f"{mmss(dead)} ({dead / duration:.0%})", "While dead a hero earns nothing and can't help the team")
    stat(c[3], "Wards placed", f"{p.get('obs_placed', 0)} obs · {p.get('sen_placed', 0)} sentry",
         "Observer wards give vision; sentry wards reveal invisible enemies and enemy wards")

    # Net worth race: the chosen hero highlighted, everyone else recedes to gray.
    st.subheader("Net worth over time")
    fig = go.Figure()
    for r in players.itertuples():
        if r.slot == row["slot"]:
            continue
        nw = r.raw.get("networth_t") or []
        fig.add_trace(go.Scatter(x=list(range(len(nw))), y=nw, mode="lines", line=dict(color=colors["muted"], width=1),
                                 opacity=0.45, name=r.hero, showlegend=False,
                                 hovertemplate=f"{r.hero} ({r.team})<br>Minute %{{x}}: %{{y:,}}<extra></extra>"))
    nw = p["networth_t"]
    fig.add_trace(go.Scatter(x=list(range(len(nw))), y=nw, mode="lines", line=dict(color=team_color, width=3),
                             name=row["hero"], showlegend=False,
                             hovertemplate=f"{row['hero']}<br>Minute %{{x}}: %{{y:,}}<extra></extra>"))
    fig.add_annotation(x=len(nw) - 1, y=nw[-1], text=row["hero"], showarrow=False, xanchor="left", xshift=6)
    fig.update_xaxes(title="Game minute")
    fig.update_yaxes(title="Net worth (gold)")
    st.caption(f"{row['hero']} in color; the other nine heroes in gray for context.")
    show(base_layout(fig, colors, 340))

    # Timeline of what this player did.
    st.subheader("Timeline: kills, deaths and big purchases")
    rows = [{"minute": k["time"] / 60, "track": "Kills", "label": f"Killed {ref['hero_by_key'].get(k['key'], k['key'])}"}
            for k in p.get("kills_log") or []]
    rows += [{"minute": d["time"] / 60, "track": "Deaths",
              "label": f"Killed by {ref['hero_by_key'].get(d['key'], d['key'].replace('npc_dota_', '').replace('_', ' '))}"
                       f" · dead {d.get('time_dead', '?')}s"} for d in p.get("deaths_log") or []]
    big = [x for x in p.get("purchase_log") or [] if ref["item_cost"].get(x["key"], 0) >= 2000]
    rows += [{"minute": x["time"] / 60, "track": "Big items (2000+ gold)",
              "label": ref["item_by_key"].get(x["key"], x["key"])} for x in big]
    tl = pd.DataFrame(rows)
    fig = go.Figure(go.Scatter(x=tl["minute"], y=tl["track"], mode="markers", marker=dict(color=team_color, size=11),
                               text=tl["label"], hovertemplate="%{text}<br>at %{x:.1f} min<extra></extra>"))
    for x in big:  # direct-label the big items only; kills and deaths stay in the tooltip
        fig.add_annotation(x=x["time"] / 60, y="Big items (2000+ gold)", text=ref["item_by_key"].get(x["key"], x["key"]),
                           showarrow=False, yshift=14, font=dict(size=11))
    fig.update_yaxes(categoryorder="array", categoryarray=["Deaths", "Kills", "Big items (2000+ gold)"])
    fig.update_xaxes(title="Game minute", range=[-1, duration / 60 + 1])
    show(base_layout(fig, colors, 260))

    # Where the gold came from.
    st.subheader("Where the gold came from")
    reasons = {}
    for code, amount in (p.get("gold_reasons") or {}).items():
        code = int(code)
        if code in NEGATIVE_REASONS:
            continue
        name = GOLD_REASONS.get(code, "Other")
        reasons[name] = reasons.get(name, 0) + amount
    gr = pd.DataFrame(sorted(reasons.items(), key=lambda kv: kv[1]), columns=["source", "gold"])
    lost = -sum(v for k, v in (p.get("gold_reasons") or {}).items() if int(k) == 1)
    left, right = st.columns([3, 1])
    with left:
        fig = go.Figure(go.Bar(x=gr["gold"], y=gr["source"], orientation="h", marker_color=team_color,
                               marker_cornerradius=4, hovertemplate="%{y}: %{x:,} gold<extra></extra>"))
        fig.update_xaxes(title="Gold earned")
        show(base_layout(fig, colors, 60 + 32 * len(gr)))
    with right:
        st.metric("Gold lost to deaths", f"{lost:,}")
        st.caption("Each death costs gold. Passive income over time is not broken down in this field.")

    with st.expander("Show the data: full purchase log"):
        st.dataframe(pd.DataFrame([{"time": mmss(x["time"]), "item": ref["item_by_key"].get(x["key"], x["key"]),
                                    "cost": ref["item_cost"].get(x["key"], 0)} for x in p.get("purchase_log") or []]),
                     hide_index=True, width="stretch")
    source_note(["networth_t", "kills_log", "deaths_log", "purchase_log", "gold_reasons", "life_state_dead"])


def section_vision(players, colors):
    st.header("6 · Vision: where the wards went")
    rows = []
    for r in players.itertuples():
        for kind, field in (("Observer", "obs_log"), ("Sentry", "sen_log")):
            for w in r.raw.get(field) or []:
                rows.append({"x": w["x"], "y": w["y"], "team": r.team, "kind": kind, "hero": r.hero, "minute": w["time"] / 60})
    if not rows:
        st.info("Ward positions need a parsed match.")
        return
    st.write(
        "Dota is played in a fog of war: you only see what your units see. **Wards** are placed to light up parts of the map. "
        "Each dot is one ward. Radiant's base is bottom-left and Dire's is top-right; the river runs diagonally between them. "
        "Circles are observer wards (vision), diamonds are sentries (reveal invisible units)."
    )
    df = pd.DataFrame(rows)
    fig = go.Figure()
    fig.add_shape(type="line", x0=64, y0=192, x1=192, y1=64, line=dict(color=colors["grid"], width=18), layer="below")
    for txt, x, y in (("Radiant base", 76, 72), ("Dire base", 180, 186), ("River", 132, 132)):
        fig.add_annotation(x=x, y=y, text=txt, showarrow=False, font=dict(color=colors["muted"], size=12))
    for team in ("Radiant", "Dire"):
        for kind, symbol in (("Observer", "circle"), ("Sentry", "diamond")):
            d = df[(df["team"] == team) & (df["kind"] == kind)]
            fig.add_trace(go.Scatter(x=d["x"], y=d["y"], mode="markers", name=f"{team} {kind.lower()}",
                                     marker=dict(color=colors[team.lower()], symbol=symbol, size=9, opacity=0.8,
                                                 line=dict(width=1, color="rgba(255,255,255,0.6)")),
                                     customdata=list(zip(d["hero"], d["minute"])),
                                     hovertemplate="%{customdata[0]}: " + kind.lower() + " at %{customdata[1]:.1f} min<extra></extra>"))
    fig.update_xaxes(range=[64, 192], showticklabels=False, showgrid=False)
    fig.update_yaxes(range=[64, 192], showticklabels=False, showgrid=False, scaleanchor="x")
    left, _ = st.columns([2, 1])
    with left:
        show(base_layout(fig, colors, 560))
    with st.expander("Show the data"):
        st.dataframe(df.groupby(["team", "hero", "kind"]).size().unstack(fill_value=0).reset_index(),
                     hide_index=True, width="stretch")
    source_note(["players[].obs_log[].x/y", "players[].sen_log[].x/y"])


def section_under_the_hood(match, players):
    st.header("7 · Under the hood: what the API actually returned")
    parsed = match.get("od_data", {}).get("has_parsed")
    p = players["raw"].iloc[0]
    nested = [k for k, v in p.items() if isinstance(v, (list, dict))]
    c = st.columns(4)
    stat(c[0], "Replay parsed?", "Yes" if parsed else "No",
         "OpenDota only parses a replay when someone requests it. Only ~2–3% of random ranked matches are parsed.")
    stat(c[1], "Fields per match", len(match))
    stat(c[2], "Fields per player", len(p), f"{len(nested)} of them are nested lists or objects")
    stat(c[3], "Response size", f"{len(json.dumps(match)) / 1024:,.0f} KB")
    st.write(
        "Everything above came from **one** API call: `GET /matches/{id}`. The response mixes three shapes of data, "
        "which is why it has to be reshaped before analysis:"
    )
    st.dataframe(pd.DataFrame([
        {"shape": "Single values", "example": "kills, gold_per_min, net_worth", "becomes": "one row per player per match (match_player)"},
        {"shape": "Per-minute arrays", "example": "gold_t, lh_t, xp_t, networth_t", "becomes": "one row per player per minute (match_player_minute)"},
        {"shape": "Event logs", "example": "kills_log, deaths_log, purchase_log, obs_log", "becomes": "one row per event (match_event)"},
    ]), hide_index=True, width="stretch")
    st.caption("Only parsed matches have the per-minute arrays and event logs. Unparsed matches have roughly 56 single-value fields per player.")
    with st.expander("Raw JSON for one player (as received)"):
        st.json(p, expanded=False)


# ---------------------------------------------------------------- page

def main():
    st.set_page_config(page_title="Match Anatomy", page_icon="⚔️", layout="wide")
    theme = getattr(getattr(st.context, "theme", None), "type", None) or "light"
    colors = THEMES.get(theme, THEMES["light"])
    ref = load_reference()

    st.title("Anatomy of a Dota 2 match")
    st.write("What one OpenDota API response tells us about a single game, explained for people who've never played.")

    matches = available_matches()
    if not matches:
        st.error("No sample matches found. Run `python scripts/explore_opendota.py` first.")
        return
    label = st.selectbox("Match", list(matches), help="Saved samples in data/samples/")
    match = _load(matches[label])
    players = build_players(match, ref)
    events = build_events(match, players)

    section_overview(match, players, ref)
    section_lineups(players)
    section_flow(match, events, colors)
    section_laning(players, colors)
    section_player(players, ref, colors)
    section_vision(players, colors)
    section_under_the_hood(match, players)


main()
