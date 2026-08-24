import os
import json
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns

# Set style for clean, professional charts
sns.set_theme(style="whitegrid")
plt.rcParams.update({'figure.autolayout': True})

def load_market_data(full_data_path="../data/full_data.json", live_market_path="../data/live_market.json"):
    """Loads and merges historical warm-up data with live trajectories."""
    with open(full_data_path, 'r') as f:
        history_data = json.load(f)
        
    with open(live_market_path, 'r') as f:
        live_data = json.load(f)

    live_dict = {tc["test_case_id"]: tc["trajectory"] for tc in live_data["test_cases"]}
    merged_sessions = {}

    for session in history_data:
        tc_id_str = session.get("testcase_id")
        if tc_id_str is None or session.get("type") != "SCORING_SESSION":
            continue
            
        tc_id = int(tc_id_str)
        live_trajectory = live_dict.get(tc_id)
        if not live_trajectory:
            continue

        # Extract history
        history = session["history"]
        hist_days = session["history_days"]
        
        # Build continuous timeline index (Negative for history, 0 to N for live)
        # History ends at Day 0, Live starts from Day 0 or 1 onwards
        hist_timeline = list(range(-hist_days, 0))
        live_timeline = [day["day"] for day in live_trajectory]
        
        # To avoid duplicating Day 0 if present in both, align cleanly:
        # History: -hist_days to -1
        hist_fed, hist_ajr, hist_thr = history["FED"], history["AJR"], history["THR"]
        
        # Live trajectory usually starts at day 0 or 1
        live_days_data = live_trajectory[1:] if live_trajectory[0]["day"] == 0 and len(hist_timeline) > 0 else live_trajectory
        
        live_timeline_clean = [d["day"] for d in live_days_data]
        live_fed = [d["FED"] for d in live_days_data]
        live_ajr = [d["AJR"] for d in live_days_data]
        live_thr = [d["THR"] for d in live_days_data]

        # Shift live timeline so it seamlessly follows history (-hist_days ... -1, 0, 1, 2 ...)
        start_live_idx = 0
        continuous_timeline = hist_timeline + list(range(start_live_idx, start_live_idx + len(live_timeline_clean)))
        
        merged_sessions[tc_id] = {
            "history_days": hist_days,
            "timeline": continuous_timeline,
            "cutoff_index": len(hist_timeline), # Index where live trading begins
            "FED": hist_fed + live_fed,
            "AJR": hist_ajr + live_ajr,
            "THR": hist_thr + live_thr
        }
        
    return merged_sessions

def generate_and_save_charts(sessions, output_dir="market_analysis_outputs"):
    """Generates and organizes multi-panel analytical charts for every test case."""
    os.makedirs(output_dir, exist_ok=True)
    
    for tc_id, data in sessions.items():
        # Create a dedicated folder for each test case
        tc_dir = os.path.join(output_dir, f"test_case_{tc_id}")
        os.makedirs(tc_dir, exist_ok=True)
        
        timeline = data["timeline"]
        cutoff = data["cutoff_index"]
        fed = data["FED"]
        ajr = data["AJR"]
        thr = data["THR"]
        
        df = pd.DataFrame({
            'Timeline': timeline,
            'FED': fed,
            'AJR': ajr,
            'THR': thr
        })
        
        # Calculate returns for analytics
        df['AJR_Return'] = np.log(df['AJR'] / df['AJR'].shift(1))
        df['THR_Return'] = np.log(df['THR'] / df['THR'].shift(1))
        
        # ---------------------------------------------------------
        # CHART 1: Price Trajectory & Macro Environment (Dual Axis)
        # ---------------------------------------------------------
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8), sharex=True)
        fig.suptitle(f"Test Case {tc_id}: Asset Pricing & Macro Trajectory", fontsize=14, fontweight='bold')
        
        # Top Panel: Tech Companies (AJR & THR)
        ax1.plot(df['Timeline'], df['AJR'], color='blue', label='AJR (AjarAI)', linewidth=2)
        ax1.set_ylabel('AjarAI Price ($)', color='blue', fontweight='bold')
        ax1.tick_params(axis='y', labelcolor='blue')
        ax1.grid(True, linestyle='--', alpha=0.5)
        
        ax1_twin = ax1.twinx()
        ax1_twin.plot(df['Timeline'], df['THR'], color='red', label='THR (Theriodic)', linewidth=2)
        ax1_twin.set_ylabel('Theriodic Price ($)', color='red', fontweight='bold')
        ax1_twin.tick_params(axis='y', labelcolor='red')
        
        # Combine legends
        lines_1, labels_1 = ax1.get_legend_handles_labels()
        lines_2, labels_2 = ax1_twin.get_legend_handles_labels()
        ax1.legend(lines_1 + lines_2, labels_1 + labels_2, loc='upper left')
        ax1.set_title("Underlying Valuations")

        # Bottom Panel: FED Interest Rate
        ax2.step(df['Timeline'], df['FED'], where='mid', color='green', linewidth=2.5, label='FED Rate (%)')
        ax2.set_ylabel('Interest Rate (%)', color='green', fontweight='bold')
        ax2.set_xlabel('Timeline (Negative = Warm-up History, Positive = Live Session)', fontweight='bold')
        ax2.tick_params(axis='y', labelcolor='green')
        ax2.grid(True, linestyle='--', alpha=0.5)
        ax2.legend(loc='upper left')
        ax2.set_title("Macro Environment (FED Funds Rate)")

        # Mark the Live Trading boundary line (Day 0 equivalent)
        if cutoff < len(df['Timeline']):
            boundary_x = df['Timeline'].iloc[cutoff]
            ax1.axvline(x=boundary_x, color='black', linestyle=':', linewidth=2, label='Live Start')
            ax2.axvline(x=boundary_x, color='black', linestyle=':', linewidth=2, label='Live Start')

        plt.tight_layout()
        plt.savefig(os.path.join(tc_dir, f"pricing_trajectory_tc_{tc_id}.png"), dpi=300)
        plt.close()

        # ---------------------------------------------------------
        # CHART 2: Advanced Analytics Dashboard (Volatility & Correlation)
        # ---------------------------------------------------------
        fig, axes = plt.subplots(1, 2, figsize=(14, 6))
        fig.suptitle(f"Test Case {tc_id}: Volatility & Sector Dynamics", fontsize=14, fontweight='bold')
        
        # Subplot A: Rolling Volatility (5-day std dev of log returns)
        df['AJR_Vol'] = df['AJR_Return'].rolling(window=5).std()
        df['THR_Vol'] = df['THR_Return'].rolling(window=5).std()
        
        axes[0].plot(df['Timeline'], df['AJR_Vol'], label='AJR Volatility', color='blue')
        axes[0].plot(df['Timeline'], df['THR_Vol'], label='THR Volatility', color='red')
        if cutoff < len(df['Timeline']):
            axes[0].axvline(x=df['Timeline'].iloc[cutoff], color='black', linestyle=':', label='Live Start')
        axes[0].set_title("5-Day Rolling Volatility")
        axes[0].set_xlabel("Timeline")
        axes[0].set_ylabel("Standard Deviation")
        axes[0].legend()
        axes[0].grid(True, linestyle='--', alpha=0.5)

        # Subplot B: Correlation Scatter (AJR Returns vs THR Returns)
        clean_returns = df.dropna(subset=['AJR_Return', 'THR_Return'])
        sns.regplot(data=clean_returns, x='AJR_Return', y='THR_Return', ax=axes[1],
                    scatter_kws={'alpha':0.7, 'color':'purple'}, line_kws={'color':'orange'})
        axes[1].set_title("Return Correlation (AJR vs THR)")
        axes[1].set_xlabel("AJR Log Return")
        axes[1].set_ylabel("THR Log Return")
        axes[1].grid(True, linestyle='--', alpha=0.5)

        plt.tight_layout()
        plt.savefig(os.path.join(tc_dir, f"analytics_dashboard_tc_{tc_id}.png"), dpi=300)
        plt.close()

    print(f"✅ Successfully processed and organized graphs for {len(sessions)} test cases into '{output_dir}/'.")

# ==========================================
# EXECUTION
# ==========================================
if __name__ == "__main__":
    # Ensure full_data.json and live_market.json are in the same directory
    market_sessions = load_market_data()
    generate_and_save_charts(market_sessions)