"""
runtime_entrypoint.py — Starts the BedrockAgentCoreApp server.

Separating the entrypoint from the handler keeps the handler testable
in isolation and matches the pattern used by the working AgentCore reference.

The bedrock-agentcore SDK starts uvicorn on 0.0.0.0:8080 exposing:
  /ping         — health check called by AgentCore to verify container is live
  /invocations  — receives payload and calls handler()
"""

from runtime_handler import app

if __name__ == "__main__":
    app.run()

