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
from matplotlib.gridspec import GridSpec

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
    
    Example with synthetic data for demonstration:
    """
    rng = np.random.default_rng(RANDOM_SEED)
    n = 10_000_000

    # Simulate a heavily left-skewed distribution:
    #   ~99% near 0, a thin tail, ~800 above 0.65
    bulk     = rng.beta(0.3, 12, size=int(n * 0.9992))          # mass near 0
    mid_tail = rng.uniform(0.10, 0.65, size=int(n * 0.0006))    # sparse mid-range
    pos_tail = rng.beta(5, 3, size=int(n * 0.0002)) * 0.35 + 0.65  # ~800 positives

    scores = np.concatenate([bulk, mid_tail, pos_tail])
    np.clip(scores, 0, 1, out=scores)
    rng.shuffle(scores)

    df = pd.DataFrame({
        "record_id": np.arange(len(scores)),
        "score": scores,
    })
    return df


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
      C) ECDF with zoomed inset on upper tail
      D) Strata allocation preview
    """
    fig = plt.figure(figsize=(16, 12), facecolor="white")
    gs = GridSpec(2, 2, hspace=0.38, wspace=0.32,
                  left=0.08, right=0.94, top=0.92, bottom=0.08)

    # ── A: Log-scale histogram ──────────────────────────────
    ax1 = fig.add_subplot(gs[0, 0])
    bins_a = np.linspace(0, 1, 101)
    ax1.hist(scores, bins=bins_a, color="#4C72B0", edgecolor="none", alpha=0.85)
    ax1.set_yscale("log")
    ax1.axvline(THRESHOLD, color="#C44E52", ls="--", lw=2, label=f"Threshold = {THRESHOLD}")
    ax1.set_xlabel("Prediction Score", fontsize=11)
    ax1.set_ylabel("Count  (log scale)", fontsize=11)
    ax1.set_title("A — Full Distribution (Log Scale)", fontsize=13, fontweight="bold")
    ax1.legend(fontsize=10)
    ax1.set_xlim(-0.02, 1.02)

    # Annotate above-threshold count
    above_n = int(np.sum(scores >= THRESHOLD))
    ax1.annotate(f"{above_n:,} above threshold",
                 xy=(THRESHOLD + 0.02, ax1.get_ylim()[1] * 0.3),
                 fontsize=10, color="#C44E52", fontweight="bold")

    # ── B: Zoomed tail histogram (linear) ───────────────────
    ax2 = fig.add_subplot(gs[0, 1])
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
                     ha="center", va="bottom", fontsize=7.5, fontweight="bold", rotation=45)

    # ── C: ECDF with inset ──────────────────────────────────
    ax3 = fig.add_subplot(gs[1, 0])
    sorted_s = np.sort(scores)
    ecdf_y = np.arange(1, len(sorted_s) + 1) / len(sorted_s)

    # Downsample for plotting efficiency (plot every 1000th point)
    step = max(1, len(sorted_s) // 10000)
    ax3.plot(sorted_s[::step], ecdf_y[::step], color="#4C72B0", lw=1.5)
    ax3.axvline(THRESHOLD, color="#C44E52", ls="--", lw=1.5)
    ax3.set_xlabel("Prediction Score", fontsize=11)
    ax3.set_ylabel("Cumulative Proportion", fontsize=11)
    ax3.set_title("C — Empirical CDF", fontsize=13, fontweight="bold")

    # Inset: zoom into 0.5 – 1.0
    ax3_inset = ax3.inset_axes([0.35, 0.15, 0.60, 0.55])
    mask = sorted_s >= 0.40
    ax3_inset.plot(sorted_s[mask][::max(1, mask.sum()//2000)],
                   ecdf_y[mask][::max(1, mask.sum()//2000)],
                   color="#4C72B0", lw=1.5)
    ax3_inset.axvline(THRESHOLD, color="#C44E52", ls="--", lw=1.2)
    ax3_inset.set_xlim(0.40, 1.0)
    frac_above = np.mean(scores >= THRESHOLD)
    ax3_inset.axhline(1 - frac_above, color="grey", ls=":", lw=1)
    ax3_inset.set_title("Zoom: 0.40 – 1.0", fontsize=9)
    ax3_inset.tick_params(labelsize=8)

    # ── D: Strata allocation bar chart ──────────────────────
    ax4 = fig.add_subplot(gs[1, 1])
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
        ax4.text(bar.get_x() + bar.get_width() / 2, bar.get_height() * 1.3,
                 f"{val:,}", ha="center", va="bottom", fontsize=8, color="#4C72B0")
    for bar, val in zip(bars2, allocations):
        ax4b.text(bar.get_x() + bar.get_width() / 2, val + 3,
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
                 fontsize=16, fontweight="bold", y=0.98)
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
