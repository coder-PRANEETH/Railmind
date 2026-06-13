import matplotlib.pyplot as plt
import networkx as nx
from data_loader import load_railway
from graph import build_network_from_data
from twin import DigitalTwin
from simulator import FutureTimelineSimulator, default_scenarios

def plot_graph_and_sim():
    # Load data
    stations, tracks = load_railway("India")

    graph = build_network_from_data(stations, tracks)
    twin = DigitalTwin(graph)
    twin.seed_trains(5)
    
    # Run sim
    simulator = FutureTimelineSimulator(twin)
    scenarios_config = default_scenarios(twin)
    result = simulator.run(scenarios_config)

    # 1. Plot Graph
    plt.figure(figsize=(12, 10))
    pos = {node: (data.get("lon", 0), data.get("lat", 0)) for node, data in twin.graph.graph.nodes(data=True)}
    
    valid_nodes = [n for n in twin.graph.graph.nodes() if n in pos]
    subgraph_full = twin.graph.graph.subgraph(valid_nodes)
    
    # Remove disconnected nodes to clean up the plot
    connected_nodes = [n for n, d in subgraph_full.degree() if d > 0]
    subgraph = subgraph_full.subgraph(connected_nodes)
    
    nx.draw_networkx_nodes(subgraph, pos, node_size=20, node_color='navy', alpha=0.9)
    
    edge_colors = []
    edge_widths = []
    for u, v, data in subgraph.edges(data=True):
        if data.get("health", 1.0) < 0.6:
            edge_colors.append('crimson')
            edge_widths.append(3)
        else:
            edge_colors.append('teal')
            edge_widths.append(1.5)
            
    nx.draw_networkx_edges(subgraph, pos, edge_color=edge_colors, width=edge_widths, alpha=0.7)
    
    # Label only major junctions (degree > 2) or a sparse subset to avoid clutter
    labels = {}
    for n in connected_nodes:
        if subgraph.degree(n) > 2:
            labels[n] = subgraph.nodes[n].get("name", n)
            
    if len(labels) < 10:
        labels = {n: subgraph.nodes[n].get("name", n) for i, n in enumerate(connected_nodes) if i % 8 == 0}
    
    # Offset labels slightly upwards for better readability
    pos_labels = {k: (v[0], v[1] + 0.05) for k, v in pos.items()}
    nx.draw_networkx_labels(subgraph, pos_labels, labels=labels, font_size=8, font_color='black', font_weight='bold')
    
    plt.title("Digital Twin Railway Network\n(Teal = Healthy, Crimson = Degraded)", fontsize=16)
    plt.axis('off')
    plt.tight_layout()
    plt.savefig('network_graph.png', dpi=300, bbox_inches='tight')
    plt.close()

    # 2. Plot Simulation Results
    plt.figure(figsize=(10, 6))
    scenarios = [s.scenario_id for s in result.scenarios]
    scores = [s.total_score for s in result.scenarios]
    delays = [s.delay_score for s in result.scenarios]
    risks = [s.risk_score for s in result.scenarios]

    x = range(len(scenarios))
    plt.bar([i - 0.2 for i in x], scores, width=0.2, label='Total Weighted Score', color='navy')
    plt.bar([i for i in x], delays, width=0.2, label='Delay Component', color='orange')
    plt.bar([i + 0.2 for i in x], risks, width=0.2, label='Risk Component', color='crimson')

    plt.xlabel('Intervention Scenario', fontsize=12)
    plt.ylabel('Evaluated Score', fontsize=12)
    plt.title('Future Simulator Outcomes (Lower is Better)', fontsize=16)
    plt.xticks(x, [f"Scenario {s}" for s in scenarios])
    
    # Annotate best scenario
    plt.axhline(y=min(scores), color='green', linestyle='--', alpha=0.5, label='Optimal Threshold')
    
    plt.legend()
    plt.tight_layout()
    plt.savefig('simulation_results.png', dpi=300, bbox_inches='tight')
    plt.close()

if __name__ == "__main__":
    plot_graph_and_sim()
