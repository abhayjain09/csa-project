# MUST change — must match the region where you enabled Nova Pro model access
aws_region = "us-east-1"

# MUST change — avoids naming collisions if you have other agents in this account
project_name = "report-iq-aswering-agent"   # e.g. "esg-agent", "water-agent"

# Option B — you already have a bucket
create_input_bucket        = false
existing_input_bucket_name = "edo-coanalyst-report-610639371721"

# Option B — you already have a role
existing_role_arn = ""

max_session_lifetime_seconds = 3600   
idle_session_timeout_seconds = 900

bedrock_model_id = "us.anthropic.claude-sonnet-5"
max_output_tokens = 8096 

tool_call_budget = 10
max_parallel_questions = 3
