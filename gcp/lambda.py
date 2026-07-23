import json
import boto3
import urllib.request
from google.auth import aws
import google.auth.transport.requests


def get_gcp_access_token(secret_name, project_id):
    """Loads the AWS WIF external_account config and exchanges it for a GCP access token."""
    client = boto3.client('secretsmanager')
    response = client.get_secret_value(SecretId=secret_name)
    creds_info = json.loads(response['SecretString'])

    scopes = ['https://www.googleapis.com/auth/cloud-platform']
    creds = aws.Credentials.from_info(creds_info, scopes=scopes)

    auth_request = google.auth.transport.requests.Request()
    creds.refresh(auth_request)

    return creds.token, project_id


def lambda_handler(event, context):
    secret_name = "GCP_Vertex_Service_Account_Key"
    gcp_project_id = "project-4c82eeff-76f7-483e-958"  # <-- replace with your actual project ID
    #sample_query = "what is the latest news regarding the Artemis moon mission?"
    sample_query = "asian paint company latest annual financial report pdf download link"
    try:
        print("1. Fetching GCP WIF credentials from Secrets Manager and exchanging for access token...")
        gcp_token, project_id = get_gcp_access_token(secret_name, gcp_project_id)
        print(f"Success! Using GCP Project ID: {project_id}")

        print(f"2. Sending query to Vertex AI: '{sample_query}'")
        location = "global"
        model_id = "gemini-3.6-flash"
        url = f"https://aiplatform.googleapis.com/v1/projects/{project_id}/locations/{location}/publishers/google/models/{model_id}:generateContent"

        payload = {
            "contents": [{"role": "user", "parts": [{"text": sample_query}]}],
            "tools": [{"google_search": {}}]
        }

        headers = {
            "Authorization": f"Bearer {gcp_token}",
            "Content-Type": "application/json"
        }

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode('utf-8'),
            headers=headers,
            method="POST"
        )

        with urllib.request.urlopen(req) as response:
            response_data = json.loads(response.read().decode('utf-8'))

        print("3. Response received from Vertex AI!")

        candidates = response_data.get('candidates', [{}])
        generated_text = candidates[0].get('content', {}).get('parts', [{}])[0].get('text', 'No text returned')

        grounding_metadata = candidates[0].get('groundingMetadata', {})
        grounding_chunks = grounding_metadata.get('groundingChunks', [])

        resolved_sources = []
        for chunk in grounding_chunks:
            web = chunk.get('web', {})
            redirect_uri = web.get('uri')
            title = web.get('title', 'Unknown source')
            if not redirect_uri:
                continue
            try:
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