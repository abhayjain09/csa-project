"""
Bedrock Converse-API tool schemas.

Each tool is described here in the format Converse expects. The dispatcher
then maps `tool_name -> handler` to actually execute them.

Design notes:
- Names are verbs (`list_documents`, `fetch_pages`) so the model uses them
  in Thought naturally.
- Descriptions are explicit about WHEN to use each tool, not just what it
  does — models over-index on names and skip descriptions otherwise.
- Every schema declares `additionalProperties: false` to catch typos in
  tool_input before we pay the round-trip.
"""
from __future__ import annotations

TOOL_SPECS: list[dict] = [
    {
        "toolSpec": {
            "name": "list_documents",
            "description": (
                "List all documents in the pageIndex with their top-level "
                "section titles. Call this FIRST for every question to see "
                "what's available. Cheap — call whenever unsure which "
                "document is relevant."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "get_outline",
            "description": (
                "Return a document's outline (titles + summaries + page "
                "ranges) up to `depth` levels deep. Default depth=1 shows "
                "only top-level sections. Use this to survey a candidate "
                "document before drilling in. DO NOT fetch pages until "
                "you've read the outline."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "doc_name": {"type": "string"},
                        "depth": {"type": "integer", "minimum": 1, "maximum": 4, "default": 1},
                    },
                    "required": ["doc_name"],
                    "additionalProperties": False,
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "expand_node",
            "description": (
                "Return the children of a specific node (identified by "
                "node_id) to `depth` levels deep. Use after get_outline to "
                "drill into a promising section without dumping the whole "
                "tree into context."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "doc_name": {"type": "string"},
                        "node_id": {"type": "string"},
                        "depth": {"type": "integer", "minimum": 1, "maximum": 4, "default": 1},
                    },
                    "required": ["doc_name", "node_id"],
                    "additionalProperties": False,
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "fetch_pages",
            "description": (
                "Fetch the actual text of a page range from a document. "
                "MAX 15 pages per call — if the section you want is larger, "
                "expand_node further first. Only call this after you've "
                "narrowed down the section via outline/expand_node. Every "
                "citation you record must come from pages you fetched here."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "doc_name": {"type": "string"},
                        "page_start": {"type": "integer", "minimum": 1},
                        "page_end": {"type": "integer", "minimum": 1},
                    },
                    "required": ["doc_name", "page_start", "page_end"],
                    "additionalProperties": False,
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "keyword_scan",
            "description": (
                "Scan node titles and summaries across a document for the "
                "given terms. Returns matching sections ranked by score. "
                "Use when the metric involves specific named things "
                "(program names, regulation IDs, facility names) that "
                "summaries might mention but generic titles won't. NOT a "
                "full-text search — only titles + summaries are scanned."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "doc_name": {"type": "string"},
                        "terms": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "maxItems": 10,
                        },
                    },
                    "required": ["doc_name", "terms"],
                    "additionalProperties": False,
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "record_citation",
            "description": (
                "Record an evidence citation. quoted_span MUST be a "
                "verbatim substring of a page you have fetched via "
                "fetch_pages — otherwise the citation is rejected. Keep "
                "quoted_span under 300 characters; excerpt the most "
                "load-bearing sentence(s). Returns a citation_id you MUST "
                "reference in submit_answer."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "doc_name": {"type": "string"},
                        "page_start": {"type": "integer", "minimum": 1},
                        "page_end": {"type": "integer", "minimum": 1},
                        "quoted_span": {"type": "string", "minLength": 5, "maxLength": 300},
                    },
                    "required": ["doc_name", "page_start", "page_end", "quoted_span"],
                    "additionalProperties": False,
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "submit_answer",
            "description": (
                "Submit the final answer and terminate the loop. `answer` "
                "must conform to the output_schema shown in the prompt. "
                "`cited_ids` must be a list of citation IDs returned by "
                "record_citation. If invoking fallback_rule with no "
                "citations, still submit — set confidence to "
                "'insufficient_evidence' and explain in `reasoning`."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "answer": {"type": "object"},
                        "cited_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "confidence": {
                            "type": "string",
                            "enum": ["high", "medium", "low", "insufficient_evidence"],
                        },
                        "reasoning": {"type": "string", "minLength": 10},
                    },
                    "required": ["answer", "cited_ids", "confidence", "reasoning"],
                    "additionalProperties": False,
                }
            },
        }
    },
]


TOOL_NAMES = frozenset(spec["toolSpec"]["name"] for spec in TOOL_SPECS)
TERMINAL_TOOL = "submit_answer"
