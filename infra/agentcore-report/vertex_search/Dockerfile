# Vertex grounded-search Lambda — container image.
# Build for the Lambda runtime arch explicitly (see DEPLOY.md):
#   docker build --platform linux/amd64 -t <ecr>:v1 .
FROM public.ecr.aws/lambda/python:3.12

# Deps first so this layer caches when only code changes.
COPY requirements.txt ${LAMBDA_TASK_ROOT}/requirements.txt
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt --target "${LAMBDA_TASK_ROOT}"

# Function code.
COPY lambda.py ${LAMBDA_TASK_ROOT}/lambda.py

# handler = <module>.<function>
CMD ["lambda.lambda_handler"]