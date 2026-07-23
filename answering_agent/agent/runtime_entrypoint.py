"""
AgentCore Runtime entrypoint using the official bedrock-agentcore SDK.

The SDK handles ALL the AgentCore HTTP contract automatically:
  - POST /invocations  (routes to @app.entrypoint)
  - GET  /ping         (handles Healthy/HealthyBusy + time_of_last_update)
  - Port 8080, host 0.0.0.0
  - Session ID header parsing
  - uvicorn startup via app.run()

We only need to implement the business logic inside @app.entrypoint.
"""
from __future__ import annotations

import logging

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from config import CONFIG
from models.schemas import RuntimePayload
from pipeline import run_pipeline
from utils.logging import setup_logging
from validation.preflight import PreflightError

setup_logging()
logger = logging.getLogger(__name__)

app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload: dict) -> dict:
    """
    Main handler — called by the SDK on every POST /invocations.

    The SDK automatically:
      - Deserialises the request body to `payload` (dict)
      - Returns HealthyBusy on /ping while this function is running
      - Serialises our return value back to JSON

    We validate the payload shape via Pydantic, run the pipeline, and return
    the result. Errors are caught and returned as structured error dicts —
    we let the SDK handle the HTTP response wrapping.
    """
    # Read session ID for logging (SDK parses the header, exposes it here).
    session_id = payload.get("_session_id", "")

    logger.info(
        "entrypoint.received",
        extra={
            "session_id": session_id,
            "run_id": payload.get("run_id"),
            "n_questions": len(payload.get("question_set", [])),
        },
    )

    try:
        runtime_payload = RuntimePayload.model_validate(payload)
    except Exception as e:
        logger.warning("entrypoint.invalid_payload", extra={"err": str(e)})
        return {"status": "error", "error_type": "invalid_payload", "message": str(e)}

    try:
        result = run_pipeline(runtime_payload, CONFIG)
        return {"status": "ok", "result": result.model_dump(mode="json")}

    except PreflightError as e:
        logger.error("entrypoint.preflight_failed", extra={"problems": e.problems})
        return {"status": "error", "error_type": "preflight_failed", "problems": e.problems}

    except Exception as e:
        logger.exception("entrypoint.pipeline_failed")
        return {"status": "error", "error_type": "pipeline_failed", "message": str(e)}


if __name__ == "__main__":
    # SDK starts uvicorn on 0.0.0.0:8080 automatically.
    app.run()
