"""
Lambda: refresh run_log_backup.sh from S3 on tagged EC2s and run backups via SSM.

Flow:
  1. Resolve target Name tags = f"{TAG_PREFIX}-{suffix}" for each suffix.
  2. describe_instances -> running instances matching those Name tags.
  3. Keep only SSM-managed + Online instances.
  4. ssm.send_command (AWS-RunShellScript, batches of 50) to:
        - aws s3 cp <S3_SCRIPT_URI> <dest>   (overwrite in place)
        - chmod u+x <dest>
        - source ENV/S3_BUCKET from the cron file
        - run backup for each component (portal, feed, apache, activemq)

Notes:
  * Deploy in the SAME region as the target instances (bucket is eu-west-1).
  * Commands run as root (AWS-RunShellScript default on Linux).
  * A `#!/bin/bash` shebang is prepended so `source <(...)` process
    substitution works (SSM's default sh/dash does not support it).
"""

import os
import logging

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Constant suffixes; full Name tag = f"{TAG_PREFIX}-{suffix}".
# Override with the SUFFIXES env var (comma-separated) if needed.
DEFAULT_SUFFIXES = [
    "portal-buyer-ECS",
    "portal-ECS",
    "data-api-cluster-ECS",
    "webserver-ASG",
    "feedserver-instance",
    "activemq-asg",
]


def _env(name, default=None, required=False):
    val = os.environ.get(name, default)
    if required and not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val


def _csv(name, default):
    raw = os.environ.get(name)
    src = raw if raw else default
    return [x.strip() for x in src.split(",") if x.strip()]


def _chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def find_instance_ids(ec2, prefix, tag_key):
    suffixes = _csv("SUFFIXES", ",".join(DEFAULT_SUFFIXES))
    names = [f"{prefix}-{s}" for s in suffixes]
    filters = [
        {"Name": f"tag:{tag_key}", "Values": names},
        {"Name": "instance-state-name", "Values": ["running"]},
    ]
    ids = []
    for page in ec2.get_paginator("describe_instances").paginate(Filters=filters):
        for res in page["Reservations"]:
            for inst in res["Instances"]:
                ids.append(inst["InstanceId"])
    return ids, names


def filter_ssm_managed(ssm, instance_ids):
    if not instance_ids:
        return []
    online = set()
    for chunk in _chunked(instance_ids, 50):
        paginator = ssm.get_paginator("describe_instance_information")
        for page in paginator.paginate(
            Filters=[{"Key": "InstanceIds", "Values": chunk}]
        ):
            for info in page["InstanceInformationList"]:
                if info.get("PingStatus") == "Online":
                    online.add(info["InstanceId"])
    return [i for i in instance_ids if i in online]


def build_commands(s3_uri):
    dest = _env("SCRIPT_DEST_PATH", "/root/run_log_backup.sh")
    cron_file = _env("CRON_FILE", "/etc/cron.d/log_backup_cron")
    log_file = _env("LOG_FILE", "/var/log/run_log_backup.log")
    components = _csv("COMPONENTS", "portal,feed,apache,activemq")

    lines = [
        "#!/bin/bash",
        "set -e",
        f"aws s3 cp {s3_uri} {dest}",
        f"chmod u+x {dest}",
        f"source <(grep -E '^(ENV|S3_BUCKET)=' {cron_file})",
        "set +e",  # let each component run independently; they log to file
    ]
    for comp in components:
        lines.append(f'{dest} "$ENV" "$S3_BUCKET" {comp} >> {log_file} 2>&1')
    return lines


def send_commands(ssm, instance_ids, commands, comment):
    exec_timeout = _env("EXECUTION_TIMEOUT", "3600")
    delivery_timeout = int(_env("DELIVERY_TIMEOUT", "600"))
    command_ids = []
    for chunk in _chunked(instance_ids, 50):
        resp = ssm.send_command(
            InstanceIds=chunk,
            DocumentName="AWS-RunShellScript",
            Comment=comment[:100],
            TimeoutSeconds=delivery_timeout,
            Parameters={
                "commands": commands,
                "executionTimeout": [exec_timeout],
            },
        )
        cid = resp["Command"]["CommandId"]
        command_ids.append(cid)
        logger.info("Sent CommandId=%s to %d instances", cid, len(chunk))
    return command_ids


def lambda_handler(event, context):
    event = event or {}
    prefix = event.get("tag_prefix") or _env("TAG_PREFIX", required=True)
    s3_uri = event.get("s3_script_uri") or _env("S3_SCRIPT_URI", required=True)
    tag_key = _env("TAG_KEY", "Name")

    # Region the target instances live in. Falls back to the Lambda's own
    # region (AWS_REGION) if TARGET_REGION is not set.
    region = (
        event.get("region")
        or os.environ.get("TARGET_REGION")
        or os.environ.get("AWS_REGION")
    )
    if not region:
        raise RuntimeError("No region: set TARGET_REGION or pass event.region")
    logger.info("Operating in region=%s", region)

    ec2 = boto3.client("ec2", region_name=region)
    ssm = boto3.client("ssm", region_name=region)

    instance_ids, resolved_names = find_instance_ids(ec2, prefix, tag_key)
    logger.info("Matched %d running instances for %s: %s",
                len(instance_ids), prefix, instance_ids)

    if not instance_ids:
        return {
            "status": "no_instances",
            "region": region,
            "tag_prefix": prefix,
            "resolved_names": resolved_names,
        }

    if _env("SKIP_SSM_CHECK", "false").lower() != "true":
        managed = filter_ssm_managed(ssm, instance_ids)
    else:
        managed = instance_ids
    print(f"SSM-managed instances: {managed}")
    skipped = sorted(set(instance_ids) - set(managed))
    if skipped:
        logger.warning("Skipping non-SSM/offline instances: %s", skipped)

    if not managed:
        return {
            "status": "no_ssm_managed_instances",
            "region": region,
            "tag_prefix": prefix,
            "matched": instance_ids,
        }

    commands = build_commands(s3_uri)
    print(f"Commands to send:\n{commands}")
    
    #command_ids = send_commands(ssm, managed, commands, f"log-backup refresh {prefix}")

    return {
        "status": "dispatched",
        "region": region,
        "tag_prefix": prefix,
        "s3_script_uri": s3_uri,
        "targeted_instances": managed,
        "skipped_instances": skipped,
        #"command_ids": command_ids,
    }