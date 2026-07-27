#!/usr/bin/env bash
# =============================================================
#  enable_https.sh — Make reportiq-internal-alb compliant with
#  ELB-024 by adding an HTTPS:443 listener (self-signed cert)
#  and converting the HTTP:80 listener to a redirect.
#
#  Run from your Mac (has openssl + aws cli).
#  Self-signed cert = no team dependency, internal-only traffic.
# =============================================================
set -euo pipefail

REGION="us-east-1"
ALB_NAME="reportiq-internal-alb"
TG_NAME="reportiq-tg"
CERT_CN="reportiq-internal"
SSL_POLICY="ELBSecurityPolicy-TLS13-1-2-2021-06"
INTERNAL_CIDRS=("10.0.0.0/8" "172.16.0.0/12" "192.168.0.0/16")

GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info() { echo -e "${GREEN}[INFO]${NC} $*"; }
step() { echo -e "\n${CYAN}═══ $* ═══${NC}"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
die()  { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ── Step 1: Discover ALB, target group, listener, SG ──────────
step "1/6  Discovering ALB resources"

ALB_ARN=$(aws elbv2 describe-load-balancers --region "$REGION" \
  --names "$ALB_NAME" \
  --query 'LoadBalancers[0].LoadBalancerArn' --output text)
[ "$ALB_ARN" = "None" ] && die "ALB $ALB_NAME not found"

ALB_DNS=$(aws elbv2 describe-load-balancers --region "$REGION" \
  --load-balancer-arns "$ALB_ARN" \
  --query 'LoadBalancers[0].DNSName' --output text)

ALB_SG=$(aws elbv2 describe-load-balancers --region "$REGION" \
  --load-balancer-arns "$ALB_ARN" \
  --query 'LoadBalancers[0].SecurityGroups[0]' --output text)

TG_ARN=$(aws elbv2 describe-target-groups --region "$REGION" \
  --names "$TG_NAME" \
  --query 'TargetGroups[0].TargetGroupArn' --output text)

HTTP_LISTENER=$(aws elbv2 describe-listeners --region "$REGION" \
  --load-balancer-arn "$ALB_ARN" \
  --query 'Listeners[?Port==`80`].ListenerArn | [0]' --output text)

info "ALB:           $ALB_ARN"
info "ALB DNS:       $ALB_DNS"
info "ALB SG:        $ALB_SG"
info "Target group:  $TG_ARN"
info "HTTP listener: $HTTP_LISTENER"

# ── Step 2: Generate self-signed cert ─────────────────────────
step "2/6  Generating self-signed certificate"

WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT

# SAN includes the ALB DNS name so browsers match the host
cat > "$WORKDIR/openssl.cnf" <<EOF
[req]
distinguished_name = dn
x509_extensions = v3_req
prompt = no
[dn]
C = US
O = S&P Global
CN = $CERT_CN
[v3_req]
subjectAltName = @alt_names
[alt_names]
DNS.1 = $CERT_CN
DNS.2 = $ALB_DNS
EOF

openssl req -x509 -nodes -days 825 -newkey rsa:2048 \
  -keyout "$WORKDIR/reportiq.key" \
  -out "$WORKDIR/reportiq.crt" \
  -config "$WORKDIR/openssl.cnf" 2>/dev/null

info "Cert generated (CN=$CERT_CN, SAN includes ALB DNS)"

# ── Step 3: Import cert to ACM ────────────────────────────────
step "3/6  Importing certificate to ACM"

CERT_ARN=$(aws acm import-certificate --region "$REGION" \
  --certificate "fileb://$WORKDIR/reportiq.crt" \
  --private-key "fileb://$WORKDIR/reportiq.key" \
  --tags Key=AppID,Value=ASP0017650 Key=CreatedBy,Value=Abhay.Lunkad Key=Name,Value=reportiq-selfsigned \
  --query 'CertificateArn' --output text)

info "Imported: $CERT_ARN"

# ── Step 4: Allow 443 on ALB security group ───────────────────
step "4/6  Opening port 443 on ALB security group"

for cidr in "${INTERNAL_CIDRS[@]}"; do
  aws ec2 authorize-security-group-ingress --region "$REGION" \
    --group-id "$ALB_SG" \
    --protocol tcp --port 443 --cidr "$cidr" 2>/dev/null \
    && info "Allowed 443 from $cidr" \
    || warn "Rule for $cidr already exists (skipping)"
done

# ── Step 5: Create HTTPS:443 listener ─────────────────────────
step "5/6  Creating HTTPS listener on 443"

EXISTING_443=$(aws elbv2 describe-listeners --region "$REGION" \
  --load-balancer-arn "$ALB_ARN" \
  --query 'Listeners[?Port==`443`].ListenerArn | [0]' --output text)

if [ "$EXISTING_443" = "None" ] || [ -z "$EXISTING_443" ]; then
  HTTPS_LISTENER=$(aws elbv2 create-listener --region "$REGION" \
    --load-balancer-arn "$ALB_ARN" \
    --protocol HTTPS --port 443 \
    --ssl-policy "$SSL_POLICY" \
    --certificates CertificateArn="$CERT_ARN" \
    --default-actions Type=forward,TargetGroupArn="$TG_ARN" \
    --query 'Listeners[0].ListenerArn' --output text)
  info "HTTPS listener created: $HTTPS_LISTENER"
else
  warn "HTTPS listener already exists: $EXISTING_443"
  aws elbv2 modify-listener --region "$REGION" \
    --listener-arn "$EXISTING_443" \
    --certificates CertificateArn="$CERT_ARN" >/dev/null
  info "Updated existing HTTPS listener cert"
fi

# ── Step 6: Convert HTTP:80 to redirect → 443 (fixes ELB-024) ─
step "6/6  Converting HTTP:80 listener to HTTPS redirect"

aws elbv2 modify-listener --region "$REGION" \
  --listener-arn "$HTTP_LISTENER" \
  --default-actions '[{"Type":"redirect","RedirectConfig":{"Protocol":"HTTPS","Port":"443","StatusCode":"HTTP_301"}}]' >/dev/null

info "Port 80 now redirects to 443 (no more HTTP forward → ELB-024 satisfied)"

# ── Done ──────────────────────────────────────────────────────
echo ""
echo -e "${CYAN}════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  HTTPS enabled — ALB is now ELB-024 compliant${NC}"
echo -e "${CYAN}════════════════════════════════════════════════════════${NC}"
echo ""
echo -e "  Open: ${YELLOW}https://$ALB_DNS${NC}"
echo ""
echo "  (Self-signed cert → browser will warn once; click Advanced →"
echo "   Proceed. Traffic is internal-only inside the VPC.)"
echo ""
echo "  Port 80 auto-redirects to 443, so http:// also works."
echo ""
echo "Verify listeners:"
echo "  aws elbv2 describe-listeners --region $REGION \\"
echo "    --load-balancer-arn $ALB_ARN \\"
echo "    --query 'Listeners[*].[Port,Protocol,DefaultActions[0].Type]' --output table"