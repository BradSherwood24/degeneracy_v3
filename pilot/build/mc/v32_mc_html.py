"""Interactive local HTML (Plotly, offline) for the V3.2 Monte Carlo results (v32_mc_results.json)."""
import json, os
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import plotly.io as pio

here = os.path.dirname(os.path.abspath(__file__))
R = json.load(open(os.path.join(here, "v32_mc_results.json")))
G = R["grid"]
EST = ["OPTIMISTIC", "BASE", "PESSIMISTIC"]
COL = {"OPTIMISTIC": "#27ae60", "BASE": "#2980b9", "PESSIMISTIC": "#c0392b"}
FAILS = ["leg-fail 0.5%", "leg-fail 2%", "leg-fail 5%"]
B0 = R["params"]["B0"]

fig = make_subplots(rows=2, cols=2, subplot_titles=(
    "30 days — terminal balance vs contracts/set (median, 5–95% band; leg-fail 2%)",
    "90 days — terminal balance vs contracts/set (median, 5–95% band; leg-fail 2%)",
    "Lock per set (ledger basis): forward sim 21 fills + live set",
    "Days until the falsifier's n=30 verdict gate (fill-rate posterior)"),
    vertical_spacing=0.16, horizontal_spacing=0.08)

for ci, days in ((1, 30), (2, 90)):
    for e in EST:
        rows = sorted([g for g in G if g["days"] == days and g["estimate"] == e and g["fail"] == "leg-fail 2%"],
                      key=lambda g: g["contracts"])
        x = [g["contracts"] for g in rows]
        fig.add_trace(go.Scatter(x=x + x[::-1], y=[g["p95"] for g in rows] + [g["p5"] for g in rows][::-1],
                                 fill="toself", fillcolor=COL[e], opacity=0.12, line=dict(width=0),
                                 showlegend=False, hoverinfo="skip", legendgroup=e), row=1, col=ci)
        fig.add_trace(go.Scatter(x=x, y=[g["median_bal"] for g in rows], mode="lines+markers", name=e, legendgroup=e,
                                 showlegend=(ci == 1), line=dict(color=COL[e], width=2.5),
                                 customdata=[[g["p5"], g["p95"], g["p_loss"], g["p_ruin"], g["p_double"],
                                              g["median_maxdd"], g["mean_sets"], g["mean_fails"]] for g in rows],
                                 hovertemplate=("%{x} contracts/set<br>median $%{y:.2f}<br>p5 $%{customdata[0]:.2f} · p95 $%{customdata[1]:.2f}"
                                                "<br>P(loss) %{customdata[2]:.1%} · P(ruin) %{customdata[3]:.1%} · P(2x) %{customdata[4]:.1%}"
                                                "<br>median maxDD %{customdata[5]:.1%} · sets %{customdata[6]:.0f} · leg-fails %{customdata[7]:.1f}"
                                                "<extra>" + e + "</extra>")), row=1, col=ci)
    fig.add_hline(y=B0, line=dict(color="#888", dash="dot"), row=1, col=ci)
    fig.add_vline(x=2, line=dict(color="#e67e22", dash="dash"), row=1, col=ci,
                  annotation_text="proxy pin: 2/order", annotation_position="top right")
    fig.update_xaxes(title_text="contracts per set (fill = min(c, taker print size))", tickvals=[1, 2, 5, 10, 20], row=1, col=ci)
    fig.update_yaxes(title_text="$", row=1, col=ci)

for e in EST:
    fig.add_trace(go.Histogram(x=R["locks"][e], name=e, legendgroup=e, showlegend=False, opacity=0.55,
                               marker_color=COL[e], xbins=dict(start=-1, end=14, size=1),
                               hovertemplate="lock %{x}c: %{y} fills<extra>" + e + "</extra>"), row=2, col=1)
fig.update_xaxes(title_text="lock per completed set (cents, ledger basis; true ≈ +1.5c)", row=2, col=1)
fig.update_yaxes(title_text="fills", row=2, col=1)

# days to n=30: rebuild the distribution from the posterior parameters
a, b = R["params"]["fill_posterior"]
rng = np.random.default_rng(7)
post = rng.beta(a, b, size=50000)
d30 = (rng.negative_binomial(30, post) + 30) / R["params"]["WINDOWS_PER_DAY"]
fig.add_trace(go.Histogram(x=d30, nbinsx=40, marker_color="#8e44ad", opacity=0.7, showlegend=False,
                           hovertemplate="%{x:.1f} days: %{y} paths<extra>days to n=30</extra>"), row=2, col=2)
med = float(np.median(d30))
fig.add_vline(x=med, line=dict(color="#333", dash="dot"), row=2, col=2, annotation_text=f"median {med:.1f} d")
fig.update_xaxes(title_text="days from 2026-09-15 to 30 completed sets", row=2, col=2)
fig.update_yaxes(title_text="paths", row=2, col=2)

fr = R["fill_rate"]
fig.update_layout(
    title=dict(text=(f"V3.2 Monte Carlo — 20,000 paths from ${B0:.2f} · E=0.10, 1 set/hour · "
                     f"fill rate {fr['fills_per_day_mean']:.2f}/day (90% CI {fr['fills_per_day_ci90'][0]:.1f}–{fr['fills_per_day_ci90'][1]:.1f}) · "
                     f"leg-failure scenarios 0.5% / 2% / 5% (NOT measured — sim: never; live: 0 of 1)"),
               font=dict(size=14)),
    barmode="overlay", height=900, template="plotly_white", legend=dict(orientation="h", y=-0.06))
out = os.path.join(here, "v32_mc_2026-09-15.html")
pio.write_html(fig, out, include_plotlyjs=True, full_html=True)
print("wrote", out, os.path.getsize(out) // 1024, "KB")
