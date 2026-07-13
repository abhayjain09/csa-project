#!/usr/bin/env python3
"""
wipe_all.py  —  Wipe ALL data from DynamoDB tables + S3 bucket.
Works on Python 3.9+. Run from your Mac.
Usage:  python3 wipe_all.py
"""
import boto3, sys
from botocore.exceptions import ClientError

REGION = "us-east-1"
BUCKET = "edo-coanalyst-report-610639371721"

# Table name → (partition_key, sort_key or None)
TABLES = [
    ("reportiq-web-queries",            "query_id", None),
    ("reportiq-runs",                   "run_id",   None),
    ("edo-coanalyst-report-provenance", "company",  "s3_key"),
]

R  = "\033[0;31m"
G  = "\033[0;32m"
C  = "\033[0;36m"
Y  = "\033[1;33m"
NC = "\033[0m"

def ok(msg):   print(f"{G}[✓]{NC} {msg}")
def warn(msg): print(f"{Y}[!]{NC} {msg}")
def step(n, msg):
    print(f"\n{C}{'='*42}{NC}")
    print(f"{C}  {n}  {msg}{NC}")
    print(f"{C}{'='*42}{NC}")


def count_items(ddb, table_name):
    try:
        return ddb.Table(table_name).scan(Select="COUNT")["Count"]
    except Exception:
        return "?"


def wipe_table(ddb, table_name, pk, sk):
    table   = ddb.Table(table_name)
    deleted = 0
    print(f"  {table_name} ...", end="", flush=True)

    while True:
        names = {"#pk": pk}
        proj  = "#pk"
        if sk:
            names["#sk"] = sk
            proj = "#pk, #sk"

        resp  = table.scan(
            ProjectionExpression=proj,
            ExpressionAttributeNames=names,
            Limit=100,
        )
        items = resp.get("Items", [])
        if not items:
            break

        with table.batch_writer() as batch:
            for item in items:
                key = {pk: item[pk]}
                if sk and sk in item:
                    key[sk] = item[sk]
                batch.delete_item(Key=key)
                deleted += 1

    ok(f"{table_name} → {deleted} items deleted")


def wipe_s3(s3):
    print("  Removing current objects ...", end="", flush=True)
    removed = 0
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=BUCKET):
            objects = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if objects:
                s3.delete_objects(Bucket=BUCKET, Delete={"Objects": objects})
                removed += len(objects)
    except ClientError as e:
        warn(str(e))
    ok(f"Current objects removed: {removed}")

    for kind in ["Versions", "DeleteMarkers"]:
        print(f"  Removing {kind} ...", end="", flush=True)
        total = 0
        while True:
            try:
                resp  = s3.list_object_versions(Bucket=BUCKET)
                batch = resp.get(kind, [])
                if not batch:
                    break
                to_del = [{"Key": i["Key"], "VersionId": i["VersionId"]} for i in batch]
                s3.delete_objects(Bucket=BUCKET, Delete={"Objects": to_del})
                total += len(to_del)
            except ClientError as e:
                warn(str(e))
                break
        ok(f"{kind} removed: {total}")


def main():
    ddb = boto3.resource("dynamodb", region_name=REGION)
    s3  = boto3.client("s3",         region_name=REGION)

    print(f"\n{R}{'='*48}{NC}")
    print(f"{R}       ⚠️   DATA WIPE — IRREVERSIBLE   ⚠️{NC}")
    print(f"{R}{'='*48}{NC}\n")
    print("  Will permanently delete:\n")
    print("  DynamoDB:")
    for (tname, pk, sk) in TABLES:
        cnt = count_items(ddb, tname)
        print(f"    • {tname}  ({cnt} items)")
    print(f"\n  S3:")
    print(f"    • s3://{BUCKET}  (all objects + versions)\n")
    print(f"{R}  Cannot be undone.{NC}\n")

    confirm = input("  Type 'WIPE' to confirm: ").strip()
    if confirm != "WIPE":
        print("Aborted.")
        sys.exit(0)

    step("1/3", "DynamoDB")
    for (tname, pk, sk) in TABLES:
        wipe_table(ddb, tname, pk, sk)

    step("2/3", "S3 bucket")
    wipe_s3(s3)

    step("3/3", "Verification")
    all_clean = True

    for (tname, pk, sk) in TABLES:
        cnt = count_items(ddb, tname)
        if cnt == 0:
            ok(f"{tname} → 0 items ✓")
        else:
            warn(f"{tname} → {cnt} remaining — run again")
            all_clean = False

    try:
        paginator = s3.get_paginator("list_objects_v2")
        s3_count  = sum(p.get("KeyCount", 0) for p in paginator.paginate(Bucket=BUCKET))
        if s3_count == 0:
            ok(f"S3 {BUCKET} → 0 objects ✓")
        else:
            warn(f"S3 {BUCKET} → {s3_count} remaining — run again")
            all_clean = False
    except ClientError as e:
        warn(f"S3 verify: {e}")

    print()
    if all_clean:
        print(f"{G}  ✅  All data wiped. Ready for a fresh start.{NC}")
    else:
        print(f"{Y}  ⚠️   Some items remain — run script again.{NC}")

    print("\n  Hard-refresh browser after wipe: Cmd+Shift+R\n")


if __name__ == "__main__":
    main()