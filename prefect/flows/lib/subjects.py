import csv
import io


def load(path: str) -> list[str]:
    """
    Read subject IDs from a CSV file. First column, one subject per row.
    Accepts a local path or an s3://bucket/key URI.
    """
    if path.startswith("s3://"):
        import boto3
        _, _, rest = path.partition("s3://")
        bucket, _, key = rest.partition("/")
        body = boto3.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read().decode()
        f = io.StringIO(body)
    else:
        f = open(path, newline="")

    with f:
        rows = [row[0].strip() for row in csv.reader(f) if row and row[0].strip()]

    # Drop header row if present (first row doesn't look like an ABCD subject ID)
    if rows and not rows[0].startswith("sub-"):
        rows = rows[1:]
    return rows
