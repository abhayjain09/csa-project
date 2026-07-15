import json
import boto3
import urllib.request
from google.oauth2 import service_account
import google.auth.transport.requests


def get_gcp_access_token(secret_name):
    """Fetches the JSON key from Secrets Manager and generates an OAuth token."""
    # 1. Initialize AWS Secrets Manager client
    client = boto3.client('secretsmanager')

    # 2. Get the secret
    response = client.get_secret_value(SecretId=secret_name)
    creds_info = json.loads(response['SecretString'])

    # 3. Generate OAuth token
    scopes = ['https://www.googleapis.com/auth/cloud-platform']
    creds = service_account.Credentials.from_service_account_info(creds_info, scopes=scopes)
    auth_request = google.auth.transport.requests.Request()
    creds.refresh(auth_request)

    # Return both the token and the project_id (extracted straight from the JSON key)
    return creds.token, creds_info.get('project_id')


def lambda_handler(event, context):
    # --- CONFIGURATION ---
    # Make sure this matches the exact name of your secret in AWS Secrets Manager
    secret_name = "GCP_Vertex_Service_Account_Key"

    # Hardcoded test query
    #sample_query = "What is the latest news regarding the Artemis moon mission?"
    sample_query = "download link for hsbc bank Code of Conduct report latest pdf"
    
    # ---------------------
    try:
        print("1. Fetching GCP Credentials from Secrets Manager...")
        gcp_token, project_id = get_gcp_access_token(secret_name)
        print(f"Success! Using GCP Project ID: {project_id}")
        print(f"2. Sending query to Vertex AI: '{sample_query}'")
        location = "us-central1"
        model_id = "gemini-2.5-pro"  # gemini-1.5-pro was retired Sept 2025 — see https://docs.cloud.google.com/vertex-ai/generative-ai/docs/learn/model-versions
        url = f"https://{location}-aiplatform.googleapis.com/v1/projects/{project_id}/locations/{location}/publishers/google/models/{model_id}:generateContent"

        # Payload with Google Search Grounding enabled
        payload = {
            "contents": [{"role": "user", "parts": [{"text": sample_query}]}],
            "tools": [{"google_search": {}}]
        }

        headers = {
            "Authorization": f"Bearer {gcp_token}",
            "Content-Type": "application/json"
        }

        # Make the POST request
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode('utf-8'),
            headers=headers,
            method="POST"
        )

        with urllib.request.urlopen(req) as response:
            response_data = json.loads(response.read().decode('utf-8'))

        print("3. Response received from Vertex AI!")
        # 4. Extract the useful parts for the test output
        candidates = response_data.get('candidates', [{}])
        generated_text = candidates[0].get('content', {}).get('parts', [{}])[0].get('text', 'No text returned')

        # Grounding Metadata contains the web links the model used
        grounding_metadata = candidates[0].get('groundingMetadata', {})
        grounding_chunks = grounding_metadata.get('groundingChunks', [])

        # The model's generated_text can contain a hallucinated/reconstructed URL
        # even when grounding is on. The AUTHORITATIVE sources are the grounding
        # chunks themselves. But those come back as vertexaisearch.cloud.google.com
        # redirect links, not the real destination — so resolve each one.
        resolved_sources = []
        for chunk in grounding_chunks:
            web = chunk.get('web', {})
            redirect_uri = web.get('uri')
            title = web.get('title', 'Unknown source')
            if not redirect_uri:
                continue
            try:
                # HEAD request following redirects to get the real final URL
                redirect_req = urllib.request.Request(redirect_uri, method="HEAD")
                with urllib.request.urlopen(redirect_req, timeout=5) as redirect_resp:
                    real_url = redirect_resp.geturl()
            except Exception as redirect_err:
                real_url = f"(could not resolve redirect: {redirect_err})"
            resolved_sources.append({"title": title, "resolved_url": real_url})

        print("--- Generated Answer (may contain hallucinated URLs — verify against resolved_sources) ---")
        print(generated_text)
        print("--- Resolved Grounding Sources (authoritative) ---")
        print(json.dumps(resolved_sources, indent=2))

        # Return a clean JSON response for the Lambda Test Console
        return {
            "statusCode": 200,
            "body": json.dumps({
                "status": "Success: Connected to GCP Vertex AI",
                "query": sample_query,
                "answer": generated_text,
                "metadata_sources_found": bool(grounding_metadata),
                "resolved_sources": resolved_sources
            })
        }
    except Exception as e:
        print(f"Execution Failed: {str(e)}")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": str(e)})
        }