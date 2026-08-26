# Sentinel agent package: the part that touches the broker.
#
# The separation is the entire design claim: strategy/ decides and proposes,
# gates/ refuses, agent/ executes. The LLM lives in agent/proposer.py and
# never sees credentials or an order tool.
