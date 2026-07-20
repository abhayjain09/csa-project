vpc_id     = "vpc-0ddfa2c6e06a4bc99"
subnet_ids = ["subnet-0eb319a33bfde7293", "subnet-0ccac81dc574f7dee"]

region                   = "us-east-1"
account_id               = "610639371721"
app_name                 = "reportiq"
image_tag                = "latest"
cpu                      = 512
memory                   = 1024
desired_count            = 1
bulk_company_concurrency = 3
cpu_architecture         = "ARM64"
assign_public_ip         = false

# These private subnets have approved public HTTPS egress through the attached
# Transit Gateway, so one-off browser tasks do not need NAT or public IPs.
enable_browser_worker = true

manage_dynamo_tables = false

# Keep true — creates ecr.api, ecr.dkr, logs endpoints.
# Reuses existing reportiq-vpce-sg if found; creates reportiq-ecs-vpce-sg if not.
# Skips S3 gateway if one already exists in the VPC.
create_vpc_endpoints = true

hosted_zone_id = "Z0486311J00RNSG5XGBS"
dns_name       = "reportiq.novavoice.spglobal.com"
