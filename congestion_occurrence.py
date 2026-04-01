"""
================================================================================
Stochastic Congestion Model – Kupferzell / Stuttgart Transmission Corridor
================================================================================
Authors  : [Jilles van der Tol & co-author]
Purpose  : Estimate a statistically rigorous, simulation-ready stochastic model
           of transmission congestion on the North→South corridor relievable by
           a GridBooster asset at Kupferzell (TransnetBW / Baden-Württemberg).

Methodology overview
--------------------
Step 1 – Data loading & preprocessing
         Raw Netztransparenz redispatch CSV → cleaned DataFrame with proper
         timestamps (15-min resolution grid).

Step 2 – Corridor congestion event extraction
         We proxy "Kupferzell-corridor congestion" as any 15-min interval in
         which TransnetBW instructed ≥1 BW load-centre asset to ramp UP.
         Ramp-up instructions by the destination TSO (south of bottleneck) are
         the canonical signal that the N→S corridor is overloaded and that
         without corrective dispatch the physical constraint would be binding.

Step 3 – Descriptive statistics & exploratory analysis
         Congestion frequency, intensity (MW), duration distribution,
         intra-day and intra-week patterns.

Step 4 – Stochastic model specification and estimation
         Model A: 2-state Hidden Markov Model (HMM)
                  Captures hysteresis and regime persistence (congested /
                  uncongested) which is fundamental to grid behaviour.
         Model B: Inhomogeneous Poisson Process (IPP) with calendar covariates
                  Log-linear GLM: ln λ(t) = β₀ + β_h·hour + β_dow·day_of_week
                  + β_m·month.  Provides interpretable rate surface.
         Model C: Hawkes Self-Exciting Process
                  λ(t) = μ(t) + Σ_{tᵢ<t} α·exp(−β(t−tᵢ))
                  Captures temporal clustering: congestion begets congestion
                  (slow redispatch resolution, queue effects).

Step 5 – Model validation & Monte Carlo simulation
         AIC/BIC comparison; simulate 10,000 synthetic weeks; recover
         empirical distribution of congestion hours per day.

Step 6 – Scalability notes
         All functions accept a DataFrame of arbitrary length.  With multi-year
         SMARD/Netztransparenz data the calendar GLM gains monthly seasonality
         and weather-wind interaction terms; the Hawkes kernel is re-estimated
         via MLE on the full event sequence.

================================================================================
"""

import warnings
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from matplotlib.colors import LinearSegmentedColormap
import seaborn as sns
from scipy import stats
from scipy.optimize import minimize
from scipy.special import gammaln
import statsmodels.api as sm
from statsmodels.tsa.statespace.mlemodel import MLEModel
from hmmlearn import hmm

# ─── Plotting style ────────────────────────────────────────────────────────────
plt.rcParams.update({
    "font.family": "serif",
    "font.size": 10,
    "axes.labelsize": 11,
    "axes.titlesize": 11,
    "legend.fontsize": 9,
    "figure.dpi": 150,
    "axes.spines.top": False,
    "axes.spines.right": False,
})
COLORS = {"congested": "#C0392B", "free": "#2980B9", "model": "#27AE60",
          "hawkes": "#8E44AD", "neutral": "#7F8C8D"}

# ══════════════════════════════════════════════════════════════════════════════
# STEP 1 – DATA LOADING & PREPROCESSING
# ══════════════════════════════════════════════════════════════════════════════

def load_redispatch(path: str) -> pd.DataFrame:
    """
    Load and clean a Netztransparenz redispatch CSV export.

    The file uses semicolon delimiters, DD.MM.YYYY date format, and
    comma decimal separators (German locale).  Power columns are cast
    to float; start/end timestamps are parsed to UTC-aware datetimes.

    Parameters
    ----------
    path : str
        Absolute path to the CSV file.

    Returns
    -------
    pd.DataFrame
        Cleaned DataFrame with added columns:
        - ``start``       : pd.Timestamp (tz-naive, interpreted as CET/CEST)
        - ``end``         : pd.Timestamp
        - ``duration_h``  : float, event duration in hours
        - ``P_mean_MW``   : float, mean power
        - ``P_max_MW``    : float, peak power
        - ``E_MWh``       : float, total energy dispatched
    """
    df = pd.read_csv(path, sep=";", encoding="utf-8-sig", dtype=str)

    # Rename for convenience
    col_map = {
        "BEGINN_DATUM": "date_start", "BEGINN_UHRZEIT": "time_start",
        "ENDE_DATUM": "date_end",   "ENDE_UHRZEIT":   "time_end",
        "GRUND_DER_MASSNAHME":   "reason",
        "RICHTUNG":              "direction",
        "MITTLERE_LEISTUNG_MW":  "P_mean_MW",
        "MAXIMALE_LEISTUNG_MW":  "P_max_MW",
        "GESAMTE_ARBEIT_MWH":    "E_MWh",
        "ANWEISENDER_UENB":      "instructing_tso",
        "ANFORDERNDER_UENB":     "requesting_tso",
        "BETROFFENE_ANLAGE":     "asset",
        "PRIMAERENERGIEART":     "fuel_type",
    }
    df = df.rename(columns=col_map)
    df = df[[c for c in col_map.values() if c in df.columns]]

    # Parse timestamps
    for col in ("P_mean_MW", "P_max_MW", "E_MWh"):
        df[col] = pd.to_numeric(df[col].str.replace(",", "."), errors="coerce")

    df["start"] = pd.to_datetime(
        df["date_start"] + " " + df["time_start"], format="%d.%m.%Y %H:%M"
    )
    df["end"] = pd.to_datetime(
        df["date_end"] + " " + df["time_end"], format="%d.%m.%Y %H:%M"
    )
    df["duration_h"] = (df["end"] - df["start"]).dt.total_seconds() / 3600

    return df.reset_index(drop=True)


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2 – CORRIDOR CONGESTION EVENT EXTRACTION
# ══════════════════════════════════════════════════════════════════════════════

# Assets geographically located south of the Kupferzell bottleneck,
# within the TransnetBW control area load centres around Stuttgart.
# When TransnetBW instructs these plants to RAMP UP it is because the
# N→S corridor is thermally constrained and additional local generation
# is needed downstream of the bottleneck.
BW_LOAD_CENTER_ASSETS = [
    "Rheinhafen-Dampfkraftwerk Karlsruhe",
    "Reservekraftwerk Rheinhafen",
    "Heizkraftwerk Heilbronn",
    "Heizkraftwerk Altbach",
    "Grosskraftwerk Mannheim",
    "Reservekraftwerk Marbach",
    "Reservekraftwerk Altbach",
    "Heizkraftwerk_Pforzheim",
    "Pumpspeicherwerk Wehr",
    "Neckar",
    "Marbach",
]

RAMP_UP_KEYWORD   = "erhöhen"    # German for "increase"
RAMP_DOWN_KEYWORD = "reduzieren" # German for "reduce"


def extract_corridor_events(df: pd.DataFrame,
                             instructing_tso: str = "TransnetBW",
                             asset_keywords: list = BW_LOAD_CENTER_ASSETS,
                             direction: str = RAMP_UP_KEYWORD
                             ) -> pd.DataFrame:
    """
    Filter redispatch records to those that proxy N→S corridor congestion.

    Selection criteria (all must be satisfied):
    1. ``instructing_tso`` == *instructing_tso*  (TransnetBW is ordering)
    2. ``asset`` matches any keyword in *asset_keywords*  (BW load-centre)
    3. ``direction`` contains *direction*  (ramp-up = inject south of bottleneck)
    4. ``reason`` == 'Strombedingter Redispatch'  (electrical, not voltage)

    For multi-year analysis, also include 'Strom- und Spannungsbedingter RD'
    which often co-occurs at this corridor.
    """
    asset_pattern = "|".join(asset_keywords)
    mask = (
        (df["instructing_tso"] == instructing_tso)
        & df["asset"].str.contains(asset_pattern, case=False, na=False)
        & df["direction"].str.contains(direction, case=False, na=False)
        & df["reason"].str.contains("Strombedingter", na=False)
    )
    return df[mask].copy()


def build_15min_grid(df_raw: pd.DataFrame,
                     df_events: pd.DataFrame) -> pd.DataFrame:
    """
    Project irregular-interval redispatch events onto a regular 15-minute grid.

    Each row of the output represents one 15-min slot.  For each slot we
    compute:
    - ``congested``     : binary (1 if ≥1 corridor event overlaps this slot)
    - ``n_events``      : number of concurrent active corridor orders
    - ``P_total_MW``    : sum of mean power across all active orders [MW]
    - ``P_max_MW``      : maximum single-order peak power active in the slot

    This representation is compatible with any downstream time-series model
    and is the natural resolution of the EPEX Spot intraday auction.
    """
    t_start = df_raw["start"].min().floor("15min")
    t_end   = df_raw["end"].max().ceil("15min")
    grid = pd.date_range(t_start, t_end, freq="15min")
    ts   = pd.DataFrame(index=grid)
    ts.index.name = "timestamp"
    ts["congested"]  = 0
    ts["n_events"]   = 0
    ts["P_total_MW"] = 0.0
    ts["P_max_MW"]   = 0.0

    for _, row in df_events.iterrows():
        s = row["start"].floor("15min")
        e = row["end"].ceil("15min")
        mask = (ts.index >= s) & (ts.index < e)
        ts.loc[mask, "congested"]  = 1
        ts.loc[mask, "n_events"]  += 1
        ts.loc[mask, "P_total_MW"] += row["P_mean_MW"]
        ts.loc[mask, "P_max_MW"]   = np.maximum(
            ts.loc[mask, "P_max_MW"], row["P_max_MW"]
        )

    # Calendar features (needed for all downstream models)
    ts["hour"]       = ts.index.hour + ts.index.minute / 60
    ts["hour_int"]   = ts.index.hour
    ts["dow"]        = ts.index.dayofweek          # 0=Mon … 6=Sun
    ts["month"]      = ts.index.month
    ts["date"]       = ts.index.date
    ts["is_weekend"] = (ts["dow"] >= 5).astype(int)

    return ts


# ══════════════════════════════════════════════════════════════════════════════
# STEP 3 – DESCRIPTIVE STATISTICS
# ══════════════════════════════════════════════════════════════════════════════

def descriptive_statistics(ts: pd.DataFrame) -> dict:
    """Compute a table of summary statistics for the congestion time series."""
    total_slots   = len(ts)
    cong_slots    = ts["congested"].sum()
    cong_frac     = cong_slots / total_slots
    n_days        = ts["date"].nunique()
    cong_h_per_day = (cong_slots * 0.25) / n_days  # 15-min slots × 0.25 h

    # Duration distribution: identify contiguous congestion episodes
    episodes = []
    in_cong  = False
    ep_start = None
    for t, row in ts.iterrows():
        if row["congested"] and not in_cong:
            in_cong  = True
            ep_start = t
        elif not row["congested"] and in_cong:
            in_cong = False
            dur = (t - ep_start).total_seconds() / 3600
            episodes.append({"start": ep_start, "end": t,
                             "duration_h": dur})
    if in_cong:  # close open episode
        episodes.append({"start": ep_start, "end": ts.index[-1],
                         "duration_h": (ts.index[-1]-ep_start).total_seconds()/3600})
    ep_df = pd.DataFrame(episodes)

    stats_dict = {
        "total_15min_slots":       total_slots,
        "congested_slots":         int(cong_slots),
        "congestion_fraction":     round(cong_frac, 4),
        "congestion_pct":          round(cong_frac * 100, 2),
        "days_observed":           n_days,
        "mean_congestion_h_per_day": round(cong_h_per_day, 2),
        "n_episodes":              len(ep_df),
        "mean_episode_h":          round(ep_df["duration_h"].mean(), 2) if len(ep_df) > 0 else 0,
        "median_episode_h":        round(ep_df["duration_h"].median(), 2) if len(ep_df) > 0 else 0,
        "max_episode_h":           round(ep_df["duration_h"].max(), 2) if len(ep_df) > 0 else 0,
        "mean_P_total_MW_when_congested": round(ts.loc[ts["congested"]==1, "P_total_MW"].mean(), 1),
        "max_P_total_MW":          round(ts["P_total_MW"].max(), 1),
    }
    return stats_dict, ep_df


# ══════════════════════════════════════════════════════════════════════════════
# STEP 4A – HIDDEN MARKOV MODEL (2-STATE)
# ══════════════════════════════════════════════════════════════════════════════

def fit_hmm(ts: pd.DataFrame, n_components: int = 2,
            n_iter: int = 200) -> dict:
    """
    Fit a Gaussian HMM to the 15-min congestion power series.

    We model the observed sequence X_t = P_total_MW (a non-negative,
    skewed series).  Because many slots have X_t=0 we use a two-component
    mixture interpretation: a Bernoulli 'congested' regime with Gaussian
    power distribution, and a 'free' regime concentrated at zero.

    In practice we add a small jitter (ε ~ N(0,1)) so the Gaussian HMM
    can distinguish zero-power slots.

    Parameters
    ----------
    n_components : int
        Number of hidden states (2 for congested / free).
    n_iter : int
        Maximum EM iterations.

    Returns
    -------
    dict with keys: model, states, transition_matrix, stationary_dist,
                    state_means, state_stds, log_likelihood, AIC, BIC
    """
    X = ts["P_total_MW"].values.reshape(-1, 1).astype(float)
    # Small Gaussian noise to avoid degenerate covariance on zero runs
    np.random.seed(42)
    X_noisy = X + np.random.normal(0, 2.0, X.shape)

    model = hmm.GaussianHMM(n_components=n_components,
                             covariance_type="full",
                             n_iter=n_iter,
                             random_state=42,
                             tol=1e-5)
    model.fit(X_noisy)
    states  = model.predict(X_noisy)
    log_lik = model.score(X_noisy)

    n_params = (n_components**2 - n_components          # transition probs
                + n_components                           # means
                + n_components)                          # variances
    T = len(X)
    AIC = -2 * log_lik + 2 * n_params
    BIC = -2 * log_lik + n_params * np.log(T)

    # Label states: higher mean = congested
    means = model.means_.flatten()
    congested_state = int(np.argmax(means))
    free_state      = 1 - congested_state

    # Stationary distribution
    eigenvalues, eigenvectors = np.linalg.eig(model.transmat_.T)
    stationary = np.real(eigenvectors[:, np.isclose(eigenvalues, 1)])
    stationary = stationary[:, 0] / stationary[:, 0].sum()

    return {
        "model":             model,
        "states":            states,
        "transition_matrix": model.transmat_,
        "stationary_dist":   stationary,
        "state_means":       means,
        "state_stds":        np.sqrt(model.covars_.flatten()),
        "congested_state":   congested_state,
        "free_state":        free_state,
        "log_likelihood":    log_lik,
        "AIC":               AIC,
        "BIC":               BIC,
        "n_params":          n_params,
    }


# ══════════════════════════════════════════════════════════════════════════════
# STEP 4B – INHOMOGENEOUS POISSON PROCESS (CALENDAR GLM)
# ══════════════════════════════════════════════════════════════════════════════

def fit_calendar_glm(ts: pd.DataFrame) -> dict:
    """
    Fit an inhomogeneous Poisson log-linear model to the binary congestion
    indicator using hour-of-day and day-of-week as covariates.

    Model:  P(congested_{t} = 1) ≈ λ(h_t, d_t) / (λ(h_t, d_t) + 1)
    via GLM with Binomial family and log link:
        logit P = β₀ + Σ_h β_h · 1[hour=h] + Σ_d β_d · 1[dow=d]

    With multi-year data, add month dummies and wind-capacity-factor
    interaction (from SMARD data) as exogenous regressors.

    Returns
    -------
    dict with GLM result, predicted congestion probability surface (24×7),
    AIC, BIC
    """
    df_glm = ts[["congested", "hour_int", "dow"]].copy()

    # One-hot encode hour of day and day of week (drop first for identification)
    hour_dummies = pd.get_dummies(df_glm["hour_int"], prefix="h", drop_first=True)
    dow_dummies  = pd.get_dummies(df_glm["dow"],      prefix="d", drop_first=True)

    X = pd.concat([hour_dummies, dow_dummies], axis=1).astype(float)
    X.insert(0, "const", 1.0)
    y = df_glm["congested"].astype(float)

    glm = sm.GLM(y, X, family=sm.families.Binomial(link=sm.families.links.Logit()))
    result = glm.fit(maxiter=200, disp=False)

    # Predicted probability surface (24 hours × 7 days)
    surface = np.zeros((24, 7))
    for h in range(24):
        for d in range(7):
            row = {"const": 1.0}
            for c in hour_dummies.columns:
                row[c] = 1.0 if c == f"h_{h}" else 0.0
            for c in dow_dummies.columns:
                row[c] = 1.0 if c == f"d_{d}" else 0.0
            x_pred = pd.DataFrame([row], columns=X.columns).fillna(0.0)
            surface[h, d] = result.predict(x_pred)[0]

    return {
        "result":    result,
        "surface":   surface,
        "AIC":       result.aic,
        "BIC":       result.bic,
        "n_params":  int(result.df_model) + 1,
    }


# ══════════════════════════════════════════════════════════════════════════════
# STEP 4C – HAWKES SELF-EXCITING PROCESS
# ══════════════════════════════════════════════════════════════════════════════

def fit_hawkes(event_times: np.ndarray,
               T: float,
               mu0: float = 0.5,
               alpha0: float = 0.3,
               beta0: float = 1.0) -> dict:
    """
    Estimate a univariate Hawkes process by Maximum Likelihood.

    Intensity:  λ(t) = μ + α · Σ_{tᵢ < t} exp(−β·(t − tᵢ))

    The log-likelihood is:
        ℓ(μ,α,β) = −μ·T + Σᵢ log λ(tᵢ) − (α/β)·Σᵢ (1 − exp(−β·(T−tᵢ)))

    Constraints: μ > 0, α ≥ 0, β > 0, α/β < 1 (stationarity).

    Parameters
    ----------
    event_times : np.ndarray
        Sorted array of event arrival times in hours from epoch.
    T : float
        Observation window length in hours.
    mu0, alpha0, beta0 : float
        Initial parameter guesses for (μ, α, β).

    Returns
    -------
    dict with estimated parameters, log-likelihood, AIC, BIC,
    branching ratio (α/β), mean intensity.
    """
    n = len(event_times)
    if n < 5:
        return {"error": "Insufficient events for Hawkes MLE (need ≥ 5)"}

    def neg_log_lik(params):
        mu, alpha, beta = params
        if mu <= 0 or alpha < 0 or beta <= 0 or alpha >= beta:
            return 1e12  # enforce stationarity α < β

        # Recursive computation of intensity at each event time
        # (Ogata 1981 recursion for efficiency)
        lam = np.zeros(n)
        R   = np.zeros(n)   # R_i = Σ_{j<i} exp(-β(tᵢ-tⱼ))
        for i in range(n):
            if i == 0:
                R[i] = 0.0
            else:
                R[i] = np.exp(-beta * (event_times[i] - event_times[i-1])) * (1 + R[i-1])
            lam[i] = mu + alpha * R[i]

        if np.any(lam <= 0):
            return 1e12

        # Compensator integral: ∫₀ᵀ λ(t)dt = μT + (α/β)·Σᵢ(1-exp(-β(T-tᵢ)))
        compensator = mu * T + (alpha / beta) * np.sum(
            1 - np.exp(-beta * (T - event_times))
        )
        ll = np.sum(np.log(lam)) - compensator
        return -ll

    res = minimize(
        neg_log_lik,
        x0=[mu0, alpha0, beta0],
        method="L-BFGS-B",
        bounds=[(1e-6, None), (1e-6, None), (1e-6, None)],
        options={"maxiter": 10000, "ftol": 1e-12, "gtol": 1e-8}
    )

    mu_hat, alpha_hat, beta_hat = res.x
    ll_hat   = -res.fun
    n_params = 3
    AIC      = -2 * ll_hat + 2 * n_params
    BIC      = -2 * ll_hat + n_params * np.log(n)
    br       = alpha_hat / beta_hat  # branching ratio

    return {
        "mu":             mu_hat,
        "alpha":          alpha_hat,
        "beta":           beta_hat,
        "log_likelihood": ll_hat,
        "AIC":            AIC,
        "BIC":            BIC,
        "n_params":       n_params,
        "branching_ratio": br,
        "mean_intensity": mu_hat / (1 - br) if br < 1 else np.nan,
        "converged":      res.success,
    }


def simulate_hawkes(mu: float, alpha: float, beta: float,
                    T: float, n_sim: int = 10000,
                    seed: int = 42) -> list:
    """
    Simulate *n_sim* realisations of the Hawkes(μ, α, β) process over [0, T].

    Uses the Ogata thinning algorithm (Ogata 1981).

    Returns
    -------
    list of np.ndarray
        Each element is a sorted array of event arrival times for one simulation.
    """
    rng = np.random.default_rng(seed)
    results = []
    for _ in range(n_sim):
        events = []
        t = 0.0
        lam_bar = mu  # upper bound on intensity

        while t < T:
            # Propose next event from homogeneous Poisson(lam_bar)
            dt = rng.exponential(1.0 / lam_bar)
            t  = t + dt
            if t >= T:
                break

            # Compute true intensity at proposed t
            if len(events) > 0:
                ev_arr = np.array(events)
                lam_t = mu + alpha * np.sum(np.exp(-beta * (t - ev_arr)))
            else:
                lam_t = mu

            # Accept / reject (thinning)
            if rng.uniform() <= lam_t / lam_bar:
                events.append(t)
                lam_bar = lam_t + alpha  # new upper bound after acceptance
            else:
                lam_bar = max(lam_t, mu)  # decay upper bound

        results.append(np.array(events))
    return results


# ══════════════════════════════════════════════════════════════════════════════
# STEP 5 – VISUALISATION
# ══════════════════════════════════════════════════════════════════════════════

def plot_all(ts, ep_df, stats_dict, hmm_res, glm_res, hawkes_res,
             simulated_events, T_hours, out_path, hawkes_events=None):
    """Produce a comprehensive 6-panel figure suitable for journal submission."""

    fig = plt.figure(figsize=(16, 20))
    gs  = gridspec.GridSpec(4, 2, figure=fig, hspace=0.48, wspace=0.38)

    # ── Panel 1: Congestion time series (stacked area) ──────────────────────
    ax1 = fig.add_subplot(gs[0, :])
    dates = ts.index
    ax1.fill_between(dates, ts["P_total_MW"], alpha=0.55,
                     color=COLORS["congested"], label="Total dispatched MW (south of bottleneck)")
    ax1.plot(dates, ts["P_total_MW"], lw=0.5, color=COLORS["congested"], alpha=0.8)
    # Overlay HMM state
    if "states" in hmm_res:
        cong_state = hmm_res["congested_state"]
        hmm_cong = (hmm_res["states"] == cong_state).astype(float)
        ax1_twin = ax1.twinx()
        ax1_twin.step(dates, hmm_cong * 0.95, where="post", lw=0.8,
                      color=COLORS["model"], alpha=0.5, label="HMM: congested state")
        ax1_twin.set_ylim(-0.05, 1.1)
        ax1_twin.set_yticks([0, 1])
        ax1_twin.set_yticklabels(["Free", "Congested"], color=COLORS["model"])
    ax1.set_xlabel("Date (CET)")
    ax1.set_ylabel("Total redispatch power [MW]")
    ax1.set_title("Panel A  –  Corridor Congestion Power & HMM Regime States\n"
                  "(TransnetBW ramp-up orders at Stuttgart / Karlsruhe / Heilbronn / Altbach load centres)",
                  fontweight="bold")
    h1, l1 = ax1.get_legend_handles_labels()
    if "states" in hmm_res:
        h2, l2 = ax1_twin.get_legend_handles_labels()
        ax1.legend(h1+h2, l1+l2, loc="upper left", framealpha=0.7)
    ax1.grid(axis="y", alpha=0.3)

    # ── Panel 2: Intra-day congestion probability (empirical) ────────────────
    ax2 = fig.add_subplot(gs[1, 0])
    hourly_prob = ts.groupby("hour_int")["congested"].mean()
    ax2.bar(hourly_prob.index, hourly_prob.values,
            color=COLORS["congested"], alpha=0.7, edgecolor="white", linewidth=0.5)
    ax2.set_xlabel("Hour of day (CET)")
    ax2.set_ylabel("Empirical congestion probability")
    ax2.set_xticks(range(0, 24, 3))
    ax2.set_title("Panel B  –  Intra-Day Congestion Profile", fontweight="bold")
    ax2.set_ylim(0, 1.05)
    ax2.axhline(stats_dict["congestion_fraction"], ls="--",
                color="grey", lw=1, label=f"Overall mean ({stats_dict['congestion_pct']:.1f}%)")
    ax2.legend()

    # ── Panel 3: Episode duration distribution ───────────────────────────────
    ax3 = fig.add_subplot(gs[1, 1])
    if len(ep_df) > 0:
        ax3.hist(ep_df["duration_h"], bins=max(5, len(ep_df)//2),
                 color=COLORS["congested"], alpha=0.75, edgecolor="white")
        # Fit exponential (memoryless baseline)
        if len(ep_df) >= 3:
            lam_fit = 1.0 / ep_df["duration_h"].mean()
            x_fit   = np.linspace(0, ep_df["duration_h"].max() * 1.1, 200)
            ax3.plot(x_fit,
                     len(ep_df) * (ep_df["duration_h"].iloc[1] - ep_df["duration_h"].iloc[0]
                                   if len(ep_df) > 1 else 1)
                     * lam_fit * np.exp(-lam_fit * x_fit),
                     color=COLORS["model"], lw=1.8, ls="--",
                     label=f"Exp(λ={lam_fit:.2f} /h)")
            ax3.legend()
    ax3.set_xlabel("Episode duration [hours]")
    ax3.set_ylabel("Count")
    ax3.set_title("Panel C  –  Congestion Episode Duration Distribution", fontweight="bold")

    # ── Panel 4: Calendar GLM probability surface ────────────────────────────
    ax4 = fig.add_subplot(gs[2, 0])
    surface = glm_res["surface"]
    dow_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    cmap = LinearSegmentedColormap.from_list(
        "cong", ["#EBF5FB", "#2980B9", "#922B21"], N=256
    )
    im = ax4.imshow(surface, aspect="auto", cmap=cmap,
                    vmin=0, vmax=min(1, surface.max() * 1.2),
                    origin="upper", extent=[-0.5, 6.5, 23.5, -0.5])
    plt.colorbar(im, ax=ax4, label="P(congested)")
    ax4.set_xticks(range(7))
    ax4.set_xticklabels(dow_labels, fontsize=8)
    ax4.set_yticks(range(0, 24, 3))
    ax4.set_ylabel("Hour of day (CET)")
    ax4.set_title("Panel D  –  Calendar GLM: Predicted Congestion\nProbability Surface (hour × day-of-week)",
                  fontweight="bold")

    # ── Panel 5: Hawkes intensity reconstruction ─────────────────────────────
    ax5 = fig.add_subplot(gs[2, 1])
    if "mu" in hawkes_res:
        mu_h  = hawkes_res["mu"]
        al_h  = hawkes_res["alpha"]
        be_h  = hawkes_res["beta"]
        t_eval = np.linspace(0, T_hours, 2000)
        ev_h   = hawkes_events if hawkes_events is not None else np.array([])
        lam_vals = np.array([
            mu_h + al_h * np.sum(np.exp(-be_h * (t_val - ev_h[ev_h < t_val])))
            for t_val in t_eval
        ])
        ax5.plot(t_eval, lam_vals, color=COLORS["hawkes"], lw=1.2,
                 label=f"Hawkes λ(t)  [μ={mu_h:.3f}, α={al_h:.3f}, β={be_h:.3f}]")
        ax5.axhline(hawkes_res["mean_intensity"], ls="--", color="grey", lw=1,
                    label=f"Mean intensity = {hawkes_res['mean_intensity']:.3f} /h")
        # Mark observed events
        for t_ev in ev_h:
            ax5.axvline(t_ev, color=COLORS["congested"], alpha=0.15, lw=0.8)
        ax5.set_xlabel("Hours from observation start")
        ax5.set_ylabel("Event intensity λ(t)  [events/hour]")
        ax5.set_title(f"Panel E  –  Hawkes Process Intensity\n"
                      f"(branching ratio α/β = {hawkes_res['branching_ratio']:.3f})",
                      fontweight="bold")
        ax5.legend(fontsize=8)
    else:
        ax5.text(0.5, 0.5, "Insufficient events\nfor Hawkes estimation",
                 ha="center", va="center", transform=ax5.transAxes, fontsize=10)
        ax5.set_title("Panel E  –  Hawkes Process Intensity", fontweight="bold")

    # ── Panel 6: Monte Carlo – simulated congestion hours/day distribution ───
    ax6 = fig.add_subplot(gs[3, :])
    if simulated_events is not None and len(simulated_events) > 0:
        T_days = T_hours / 24
        # Convert event counts to congestion hours (each event ≈ mean episode duration)
        mean_ep_h = stats_dict["mean_episode_h"] if stats_dict["mean_episode_h"] > 0 else 1.0
        sim_cong_h_per_day = np.array([
            min(len(ev) * mean_ep_h, T_hours) / T_days
            for ev in simulated_events
        ])
        obs_mean = stats_dict["mean_congestion_h_per_day"]
        ax6.hist(sim_cong_h_per_day, bins=60, density=True,
                 color=COLORS["hawkes"], alpha=0.65, edgecolor="white", linewidth=0.3,
                 label=f"Simulated (N={len(simulated_events):,} paths)")
        ax6.axvline(obs_mean, color=COLORS["congested"], lw=2, ls="--",
                    label=f"Observed mean: {obs_mean:.1f} h/day")
        p5,  p95 = np.percentile(sim_cong_h_per_day, [5, 95])
        ax6.axvspan(p5, p95, alpha=0.15, color=COLORS["hawkes"],
                    label=f"90% CI  [{p5:.1f}, {p95:.1f}] h/day")
        sim_cong_h_per_day_filt = sim_cong_h_per_day[
            np.isfinite(sim_cong_h_per_day) & (sim_cong_h_per_day > 0)
        ]
        if len(sim_cong_h_per_day_filt) > 10:
            try:
                a_g, loc_g, scale_g = stats.gamma.fit(sim_cong_h_per_day_filt, floc=0)
                x_g = np.linspace(0, sim_cong_h_per_day_filt.max(), 300)
                ax6.plot(x_g, stats.gamma.pdf(x_g, a_g, loc=loc_g, scale=scale_g),
                         color=COLORS["model"], lw=2,
                         label=f"Fitted Γ(α={a_g:.2f}, θ={scale_g:.2f})")
            except Exception:
                pass
        ax6.set_xlabel("Simulated congestion hours per day [h]")
        ax6.set_ylabel("Density")
        ax6.set_title("Panel F  –  Monte Carlo Distribution of Congestion Hours per Day\n"
                      "(Hawkes process, 10,000 simulated weeks; Gamma fit for analytical tractability)",
                      fontweight="bold")
        ax6.legend(fontsize=9)
    # Global title
    fig.suptitle(
        "Stochastic Congestion Model  –  Kupferzell / Stuttgart Transmission Corridor\n"
        "TransnetBW ramp-up redispatch as congestion proxy  |  Jan 1–9 2026",
        fontsize=13, fontweight="bold", y=0.995
    )
    plt.savefig(out_path, bbox_inches="tight", dpi=180)
    plt.close()
    print(f"  → Figure saved: {out_path}")


# ══════════════════════════════════════════════════════════════════════════════
# STEP 6 – MODEL COMPARISON TABLE
# ══════════════════════════════════════════════════════════════════════════════

def model_comparison_table(hmm_res, glm_res, hawkes_res) -> pd.DataFrame:
    """Return a LaTeX-ready model comparison table (AIC, BIC, n_params)."""
    rows = []
    if "AIC" in hmm_res:
        rows.append({
            "Model": "2-State Gaussian HMM",
            "Parameters": hmm_res["n_params"],
            "Log-Likelihood": round(hmm_res["log_likelihood"], 2),
            "AIC": round(hmm_res["AIC"], 2),
            "BIC": round(hmm_res["BIC"], 2),
        })
    if "AIC" in glm_res:
        rows.append({
            "Model": "Inhomogeneous Poisson GLM (calendar covariates)",
            "Parameters": glm_res["n_params"],
            "Log-Likelihood": round(-glm_res["AIC"]/2 + glm_res["n_params"], 2),
            "AIC": round(glm_res["AIC"], 2),
            "BIC": round(glm_res["BIC"], 2),
        })
    if "AIC" in hawkes_res:
        rows.append({
            "Model": "Hawkes Self-Exciting Process",
            "Parameters": hawkes_res["n_params"],
            "Log-Likelihood": round(hawkes_res["log_likelihood"], 2),
            "AIC": round(hawkes_res["AIC"], 2),
            "BIC": round(hawkes_res["BIC"], 2),
        })
    return pd.DataFrame(rows)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

def main(csv_path: str, out_dir: str = "/mnt/user-data/outputs"):
    import os
    os.makedirs(out_dir, exist_ok=True)

    print("=" * 70)
    print("KUPFERZELL CORRIDOR – STOCHASTIC CONGESTION MODEL")
    print("=" * 70)

    # ── 1. Load ──────────────────────────────────────────────────────────────
    print("\n[Step 1] Loading redispatch data …")
    df = load_redispatch(csv_path)
    print(f"  Total records: {len(df)}")

    # ── 2. Extract corridor events ───────────────────────────────────────────
    print("\n[Step 2] Extracting Kupferzell corridor congestion events …")
    df_events = extract_corridor_events(df)
    print(f"  Corridor events (TransnetBW ramp-up, BW load centres): {len(df_events)}")
    print(f"  Assets activated: {df_events['asset'].nunique()}")
    print(f"  Asset list: {df_events['asset'].unique().tolist()}")

    ts = build_15min_grid(df, df_events)
    print(f"  15-min time grid: {len(ts)} slots "
          f"({ts.index[0].date()} → {ts.index[-1].date()})")

    # ── 3. Descriptive statistics ────────────────────────────────────────────
    print("\n[Step 3] Descriptive statistics …")
    stats_dict, ep_df = descriptive_statistics(ts)
    for k, v in stats_dict.items():
        print(f"  {k:45s}: {v}")

    # ── 4a. HMM ─────────────────────────────────────────────────────────────
    print("\n[Step 4a] Fitting 2-state Hidden Markov Model …")
    hmm_res = fit_hmm(ts)
    print(f"  Log-likelihood : {hmm_res['log_likelihood']:.2f}")
    print(f"  AIC / BIC      : {hmm_res['AIC']:.2f} / {hmm_res['BIC']:.2f}")
    print(f"  State means    : Free={hmm_res['state_means'][hmm_res['free_state']]:.1f} MW,"
          f"  Congested={hmm_res['state_means'][hmm_res['congested_state']]:.1f} MW")
    tm = hmm_res["transition_matrix"]
    cs = hmm_res["congested_state"]
    fs = hmm_res["free_state"]
    print(f"  P(stay congested) = {tm[cs,cs]:.3f}   "
          f"P(stay free) = {tm[fs,fs]:.3f}")

    # ── 4b. Calendar GLM ─────────────────────────────────────────────────────
    print("\n[Step 4b] Fitting inhomogeneous Poisson GLM (hour × day-of-week) …")
    glm_res = fit_calendar_glm(ts)
    print(f"  AIC / BIC      : {glm_res['AIC']:.2f} / {glm_res['BIC']:.2f}")
    print(f"  Peak congestion probability: {glm_res['surface'].max():.3f}"
          f"  (hour {glm_res['surface'].max(axis=1).argmax():02d}:00,"
          f"  {['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][glm_res['surface'].max(axis=0).argmax()]})")

    # ── 4c. Hawkes process ───────────────────────────────────────────────────
    # Event definition for Hawkes: onset of each individual redispatch ORDER
    # (not every congested 15-min slot).  This captures the self-exciting
    # dynamic: one TSO order triggers further orders on adjacent assets.
    print("\n[Step 4c] Fitting Hawkes self-exciting process …")
    t0     = ts.index[0]
    T_h    = (ts.index[-1] - t0).total_seconds() / 3600
    # Use unique order start times from the filtered corridor events
    order_starts = np.sort(np.array([
        (t - t0).total_seconds() / 3600
        for t in df_events["start"]
        if t >= t0 and t <= ts.index[-1]
    ]))
    ev_arr = order_starts
    print(f"  Using {len(ev_arr)} redispatch order onsets as Hawkes events")
    hawkes_res = fit_hawkes(ev_arr, T=T_h)
    if "error" not in hawkes_res:
        print(f"  μ={hawkes_res['mu']:.4f}  α={hawkes_res['alpha']:.4f}  β={hawkes_res['beta']:.4f}")
        print(f"  Branching ratio α/β  = {hawkes_res['branching_ratio']:.3f}")
        print(f"  Mean intensity       = {hawkes_res['mean_intensity']:.4f} events/h")
        print(f"  AIC / BIC            = {hawkes_res['AIC']:.2f} / {hawkes_res['BIC']:.2f}")
        print(f"  Converged            : {hawkes_res['converged']}")

    if "mu" in hawkes_res:
        # ── 5. Monte Carlo simulation ────────────────────────────────────────
        print("\n[Step 5] Running Hawkes Monte Carlo (10,000 paths) …")
        sim = simulate_hawkes(hawkes_res["mu"], hawkes_res["alpha"],
                              hawkes_res["beta"], T=T_h, n_sim=10000)
        n_cong_sim = [len(s) for s in sim]
        print(f"  Simulated events/path: mean={np.mean(n_cong_sim):.1f},"
              f"  p5={np.percentile(n_cong_sim,5):.0f},"
              f"  p95={np.percentile(n_cong_sim,95):.0f}")
    else:
        print(f"  {hawkes_res['error']}")
        sim = None

    # ── Model comparison ─────────────────────────────────────────────────────
    print("\n[Model Comparison]")
    cmp = model_comparison_table(hmm_res, glm_res, hawkes_res)
    print(cmp.to_string(index=False))

    # ── 6. Figures ───────────────────────────────────────────────────────────
    print("\n[Step 6] Generating figures …")
    fig_path = f"{out_dir}/kupferzell_congestion_model.png"
    plot_all(ts, ep_df, stats_dict, hmm_res, glm_res, hawkes_res,
             sim, T_h, fig_path, hawkes_events=ev_arr)

    # ── Save model comparison CSV ─────────────────────────────────────────────
    cmp_path = f"{out_dir}/model_comparison.csv"
    cmp.to_csv(cmp_path, index=False)
    print(f"  → Model comparison table saved: {cmp_path}")

    # ── LaTeX model comparison snippet ───────────────────────────────────────
    latex_path = f"{out_dir}/model_comparison.tex"
    with open(latex_path, "w") as f:
        f.write("% Model comparison table – Kupferzell corridor congestion models\n")
        f.write("% Generated by kupferzell_congestion_model.py\n\n")
        f.write(cmp.to_latex(index=False, escape=False,
                              float_format="%.2f",
                              caption="Model comparison for the Kupferzell--Stuttgart "
                                      "corridor congestion stochastic model. "
                                      "Sample: 1--9 January 2026.",
                              label="tab:model_comparison"))
    print(f"  → LaTeX table saved: {latex_path}")

    print("\n" + "="*70)
    print("PIPELINE COMPLETE")
    print("="*70)

    return {
        "df": df, "df_events": df_events, "ts": ts,
        "stats": stats_dict, "episodes": ep_df,
        "hmm": hmm_res, "glm": glm_res, "hawkes": hawkes_res,
        "simulations": sim,
    }


if __name__ == "__main__":
    CSV_PATH = "/mnt/user-data/uploads/Redispatch_Daten__3_.csv"
    results  = main(CSV_PATH)   