"""
Phase 2 — Score Distribution Analysis & Stratified Sampling
============================================================
Visualize prediction score distribution from new-month 10M run,
print summary statistics, and draw a stratified sample of 400 for FIU review.

Usage:
    1. Replace `load_scores()` with your actual data loading logic.
    2. Adjust THRESHOLD and STRATA_CONFIG as needed.
    3. Run:  python phase2_score_analysis.py
"""

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────
THRESHOLD = 0.65
RANDOM_SEED = 42
TOTAL_SAMPLE_BUDGET = 400

# Strata definitions: (label, lower_bound, upper_bound, allocation)
STRATA_CONFIG = [
    ("S1: Above Threshold",   0.65, 1.001, 160),   # predicted positives
    ("S2: Near Threshold",    0.40, 0.65,  160),   # decision-boundary zone
    ("S3: Mid-Range",         0.10, 0.40,   50),   # smoke test — deeper negatives
    ("S4: Low Scores",        0.00, 0.10,   30),   # sanity check
]

# ─────────────────────────────────────────────────────────────
# DATA LOADING  (replace with your actual loader)
# ─────────────────────────────────────────────────────────────
def load_scores() -> pd.DataFrame:
    """
    Return a DataFrame with at least a 'score' column (float 0–1).
    Include any ID / memo columns you need for downstream sampling.
    
    ── REPLACE THIS with your actual data loading logic ──
    Example:
        df = pd.read_parquet("new_month_scores.parquet")
        # or
        df = pd.read_csv("new_month_scores.csv")
        # ensure there's a 'score' column and a 'record_id' column
        return df
    """
    raise NotImplementedError(
        "Replace load_scores() with your actual data loading logic. "
        "Must return a DataFrame with at least 'record_id' and 'score' columns."
    )


# ─────────────────────────────────────────────────────────────
# 1. DISTRIBUTION SUMMARY TABLE
# ─────────────────────────────────────────────────────────────
def print_distribution_summary(scores: np.ndarray):
    """Print a binned count table plus key percentiles."""

    bins = [0, 0.01, 0.05, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60,
            0.65, 0.70, 0.80, 0.90, 1.001]
    counts, _ = np.histogram(scores, bins=bins)
    total = len(scores)

    print("\n" + "=" * 72)
    print("SCORE DISTRIBUTION — BINNED COUNTS")
    print("=" * 72)
    print(f"{'Bin':>16s}  {'Count':>12s}  {'% of Total':>10s}  {'Cumul %':>9s}")
    print("-" * 72)

    cumul = 0
    for i, c in enumerate(counts):
        lo, hi = bins[i], bins[i + 1]
        hi_label = min(hi, 1.0)
        cumul += c
        marker = "  ◄ THRESHOLD" if bins[i + 1] == 0.65 + 0.001 or (bins[i] <= THRESHOLD < bins[i + 1]) else ""
        # Mark the bin that contains the threshold
        if bins[i] <= THRESHOLD < bins[i + 1]:
            marker = "  ◄ THRESHOLD"
        elif abs(bins[i] - THRESHOLD) < 1e-9:
            marker = "  ◄ THRESHOLD"
        else:
            marker = ""
        print(f"  [{lo:5.2f}, {hi_label:5.2f})  {c:>12,d}  {c/total*100:>9.4f}%  {cumul/total*100:>8.3f}%{marker}")

    print("-" * 72)
    print(f"  {'TOTAL':>14s}  {total:>12,d}")

    # Tail focus
    above = np.sum(scores >= THRESHOLD)
    near  = np.sum((scores >= 0.40) & (scores < THRESHOLD))
    print(f"\n  Scores >= {THRESHOLD}:  {above:>10,d}  ({above/total*100:.5f}%)")
    print(f"  Scores in [0.40, {THRESHOLD}):  {near:>8,d}  ({near/total*100:.5f}%)")

    # Key percentiles
    print("\n  Key Percentiles:")
    for p in [50, 75, 90, 95, 99, 99.5, 99.9, 99.95, 99.99]:
        val = np.percentile(scores, p)
        print(f"    P{p:<6}  =  {val:.6f}")
    print("=" * 72)


# ─────────────────────────────────────────────────────────────
# 2. PLOTS
# ─────────────────────────────────────────────────────────────
def plot_distribution(scores: np.ndarray, save_path: str = "phase2_score_distribution.png"):
    """
    Four-panel figure:
      A) Log-scale histogram (full range)
      B) Zoomed linear histogram (0.3 – 1.0, the "tail")
      C) Reverse cumulative count — "records scoring >= X" (log y)
      D) Strata allocation preview
    """
    fig, axes = plt.subplots(2, 2, figsize=(16, 11))
    fig.subplots_adjust(hspace=0.40, wspace=0.34,
                        left=0.08, right=0.94, top=0.91, bottom=0.07)

    # ── A: Log-scale histogram ──────────────────────────────
    ax1 = axes[0, 0]
    bins_a = np.linspace(0, 1, 101)
    ax1.hist(scores, bins=bins_a, color="#4C72B0", edgecolor="none", alpha=0.85)
    ax1.set_yscale("log")
    ax1.axvline(THRESHOLD, color="#C44E52", ls="--", lw=2, label=f"Threshold = {THRESHOLD}")
    ax1.set_xlabel("Prediction Score", fontsize=11)
    ax1.set_ylabel("Count  (log scale)", fontsize=11)
    ax1.set_title("A — Full Distribution (Log Scale)", fontsize=13, fontweight="bold")
    ax1.legend(fontsize=10, loc="upper right")
    ax1.set_xlim(-0.02, 1.02)

    # Annotate above-threshold count (place inside axes bounds)
    above_n = int(np.sum(scores >= THRESHOLD))
    ylo, yhi = ax1.get_ylim()
    ax1.text(THRESHOLD + 0.03, yhi * 0.08, f"{above_n:,}\nabove threshold",
             fontsize=9.5, color="#C44E52", fontweight="bold", va="top")

    # ── B: Zoomed tail histogram (linear) ───────────────────
    ax2 = axes[0, 1]
    tail_scores = scores[scores >= 0.30]
    bins_b = np.linspace(0.30, 1.0, 71)
    ax2.hist(tail_scores, bins=bins_b, color="#55A868", edgecolor="white", lw=0.3)
    ax2.axvline(THRESHOLD, color="#C44E52", ls="--", lw=2, label=f"Threshold = {THRESHOLD}")
    ax2.set_xlabel("Prediction Score", fontsize=11)
    ax2.set_ylabel("Count", fontsize=11)
    ax2.set_title("B — Tail Zoom [0.30 – 1.0] (Linear Scale)", fontsize=13, fontweight="bold")
    ax2.legend(fontsize=10)

    # Add count annotations in tail bins
    bin_edges_annot = [0.30, 0.40, 0.50, 0.60, 0.65, 0.70, 0.80, 0.90, 1.001]
    for i in range(len(bin_edges_annot) - 1):
        lo, hi = bin_edges_annot[i], bin_edges_annot[i + 1]
        cnt = int(np.sum((scores >= lo) & (scores < hi)))
        if cnt > 0:
            mid = (lo + hi) / 2
            ax2.text(mid, cnt + max(1, cnt * 0.08), f"{cnt:,}",
                     ha="center", va="bottom", fontsize=7.5, fontweight="bold", rotation=45,
                     clip_on=True)

    # ── C: Reverse Cumulative Count (Survival) ──────────────
    # "If I set the threshold to X, how many records get flagged?"
    ax3 = axes[1, 0]
    sorted_s = np.sort(scores)[::-1]  # descending
    rev_cum_y = np.arange(1, len(sorted_s) + 1)

    # Downsample for plot efficiency but keep tail detail
    # Use log-spaced indices so the tail (small counts) gets more points
    n = len(sorted_s)
    idx = np.unique(np.concatenate([
        np.geomspace(1, n, num=5000, dtype=int) - 1,   # log-spaced for tail
        np.linspace(0, n - 1, num=5000, dtype=int),     # uniform for bulk
    ]))
    idx = np.sort(idx)

    ax3.plot(sorted_s[idx], rev_cum_y[idx], color="#4C72B0", lw=1.8)
    ax3.set_yscale("log")
    ax3.axvline(THRESHOLD, color="#C44E52", ls="--", lw=2)
    ax3.set_xlabel("Score Threshold (X)", fontsize=11)
    ax3.set_ylabel("Records with score ≥ X  (log)", fontsize=11)
    ax3.set_title("C — Reverse Cumulative Count", fontsize=13, fontweight="bold")
    ax3.set_xlim(-0.02, 1.02)

    # Annotate key readoff points
    readoff_thresholds = [0.10, 0.30, 0.50, THRESHOLD, 0.80]
    for t in readoff_thresholds:
        cnt = int(np.sum(scores >= t))
        if cnt > 0:
            color = "#C44E52" if t == THRESHOLD else "#555555"
            weight = "bold" if t == THRESHOLD else "normal"
            ax3.plot(t, cnt, "o", color=color, ms=5, zorder=5)
            # Offset label to avoid overlap
            y_offset = 2.0 if t != THRESHOLD else 2.5
            ax3.annotate(f"{cnt:,}", xy=(t, cnt), fontsize=8.5, color=color,
                         fontweight=weight, ha="center",
                         xytext=(0, 10), textcoords="offset points")

    ax3.text(0.03, 0.05, '"If threshold = X,\n how many flagged?"',
             transform=ax3.transAxes, fontsize=9, style="italic",
             color="#666666", va="bottom")

    # ── D: Strata allocation bar chart ──────────────────────
    ax4 = axes[1, 1]
    labels, populations, allocations = [], [], []
    for name, lo, hi, alloc in STRATA_CONFIG:
        cnt = int(np.sum((scores >= lo) & (scores < hi)))
        labels.append(name)
        populations.append(cnt)
        allocations.append(alloc)

    x = np.arange(len(labels))
    width = 0.35

    # Twin axes: population on left, allocation on right
    bars1 = ax4.bar(x - width / 2, populations, width, label="Population",
                    color="#4C72B0", alpha=0.7)
    ax4.set_yscale("log")
    ax4.set_ylabel("Population (log)", fontsize=11, color="#4C72B0")
    ax4.tick_params(axis="y", labelcolor="#4C72B0")

    ax4b = ax4.twinx()
    bars2 = ax4b.bar(x + width / 2, allocations, width, label="Sample Alloc.",
                     color="#DD8452", alpha=0.85)
    ax4b.set_ylabel("Sample Allocation", fontsize=11, color="#DD8452")
    ax4b.tick_params(axis="y", labelcolor="#DD8452")

    # Annotate bars
    for bar, val in zip(bars1, populations):
        ax4.text(bar.get_x() + bar.get_width() / 2, bar.get_height() * 1.4,
                 f"{val:,}", ha="center", va="bottom", fontsize=8, color="#4C72B0")
    for bar, val in zip(bars2, allocations):
        ax4b.text(bar.get_x() + bar.get_width() / 2, val + 4,
                  str(val), ha="center", va="bottom", fontsize=9,
                  fontweight="bold", color="#DD8452")

    ax4.set_xticks(x)
    ax4.set_xticklabels([l.replace(": ", ":\n") for l in labels], fontsize=9)
    ax4.set_title("D — Strata: Population vs Sample Allocation", fontsize=13, fontweight="bold")

    # Combined legend
    lines1, labs1 = ax4.get_legend_handles_labels()
    lines2, labs2 = ax4b.get_legend_handles_labels()
    ax4.legend(lines1 + lines2, labs1 + labs2, fontsize=10, loc="upper left")

    fig.suptitle("Phase 2 — New Month Score Distribution & Sampling Plan",
                 fontsize=15, fontweight="bold")
    plt.savefig(save_path, dpi=180, bbox_inches="tight")
    print(f"\n[✓] Plot saved → {save_path}")
    plt.close()


# ─────────────────────────────────────────────────────────────
# 3. STRATIFIED SAMPLING
# ─────────────────────────────────────────────────────────────
def stratified_sample(df: pd.DataFrame, save_path: str = "phase2_fiu_sample_400.csv"):
    """Draw stratified sample and export to CSV for FIU review."""

    rng = np.random.default_rng(RANDOM_SEED)
    samples = []

    print("\n" + "=" * 72)
    print("STRATIFIED SAMPLING PLAN")
    print("=" * 72)
    print(f"{'Stratum':<28s} {'Population':>12s} {'Requested':>10s} {'Sampled':>8s} {'Sampling Rate':>14s}")
    print("-" * 72)

    for name, lo, hi, alloc in STRATA_CONFIG:
        mask = (df["score"] >= lo) & (df["score"] < hi)
        pool = df[mask]
        n_pool = len(pool)
        n_draw = min(alloc, n_pool)  # can't sample more than available

        if n_draw > 0:
            chosen = pool.sample(n=n_draw, random_state=rng.integers(1e9))
            chosen = chosen.copy()
            chosen["stratum"] = name
            chosen["stratum_lo"] = lo
            chosen["stratum_hi"] = min(hi, 1.0)
            samples.append(chosen)

        rate = n_draw / n_pool if n_pool > 0 else 0
        print(f"  {name:<26s} {n_pool:>12,d} {alloc:>10d} {n_draw:>8d} {rate:>13.6%}")

    print("-" * 72)
    total_sampled = sum(len(s) for s in samples)
    print(f"  {'TOTAL':<26s} {len(df):>12,d} {TOTAL_SAMPLE_BUDGET:>10d} {total_sampled:>8d}")
    print("=" * 72)

    result = pd.concat(samples, ignore_index=True)
    result = result.sort_values("score", ascending=False).reset_index(drop=True)
    result.to_csv(save_path, index=False)
    print(f"\n[✓] FIU sample saved → {save_path}")
    print(f"    Columns: {list(result.columns)}")

    # Quick sanity stats on the sample
    print("\n  Sample score statistics by stratum:")
    for name, lo, hi, alloc in STRATA_CONFIG:
        subset = result[result["stratum"] == name]["score"]
        if len(subset) > 0:
            print(f"    {name:26s}  n={len(subset):>4d}  "
                  f"mean={subset.mean():.4f}  min={subset.min():.4f}  max={subset.max():.4f}")

    return result


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Loading scores...")
    df = load_scores()
    scores = df["score"].values
    print(f"  Loaded {len(scores):,d} records.")

    # 1. Summary table
    print_distribution_summary(scores)

    # 2. Plots
    plot_distribution(scores, save_path="phase2_score_distribution.png")

    # 3. Stratified sample
    sample_df = stratified_sample(df, save_path="phase2_fiu_sample_400.csv")
