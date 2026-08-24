import json
import matplotlib.pyplot as plt

def visualize_market_patterns(filepath='full_data.json'):
    # Load the live market trajectories
    with open(filepath, 'r') as f:
        data = json.load(f)

    for tc in data['test_cases']:
        tc_id = tc['test_case_id']
        days = [step['day'] for step in tc['trajectory']]
        fed = [step['FED'] for step in tc['trajectory']]
        ajr = [step['AJR'] for step in tc['trajectory']]
        thr = [step['THR'] for step in tc['trajectory']]

        # Set up the figure and dual axes
        fig, ax1 = plt.subplots(figsize=(12, 6))
        ax2 = ax1.twinx()

        # Plot AJR and THR on the primary Y-axis (Company Valuation)
        ax1.plot(days, ajr, label='AJR Valuation', color='blue', linewidth=2)
        ax1.plot(days, thr, label='THR Valuation', color='green', linewidth=2)
        
        # Plot FED on the secondary Y-axis (Interest Rate)
        ax2.step(days, fed, label='FED Rate', color='red', linestyle='--', linewidth=2, where='post')

        # Formatting and Labels
        ax1.set_xlabel('Simulation Day', fontweight='bold')
        ax1.set_ylabel('Company Valuation ($)', fontweight='bold')
        ax2.set_ylabel('FED Rate (%)', color='red', fontweight='bold')
        
        ax1.set_title(f'Market Trajectory - Testcase {tc_id}', fontsize=14, fontweight='bold')
        ax1.grid(True, alpha=0.3)
        
        # Combine legends
        lines_1, labels_1 = ax1.get_legend_handles_labels()
        lines_2, labels_2 = ax2.get_legend_handles_labels()
        ax1.legend(lines_1 + lines_2, labels_1 + labels_2, loc='upper left')

        plt.tight_layout()
        plt.show()

if __name__ == "__main__":
    visualize_market_patterns()