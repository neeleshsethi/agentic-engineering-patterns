# DynamoDB Locks Example

This example shows the minimum shape of a DynamoDB-backed resume claim.

The claim prevents two API replicas from resuming the same LangGraph `thread_id` concurrently.

## Acquire

```python
token = str(uuid.uuid4())
now = int(time.time())

table.put_item(
    Item={
        "PK": f"CLAIM#{thread_id}",
        "SK": "RESUME",
        "claim_token": token,
        "expires_at": now + 1800,
        "ttl": now + 3600,
    },
    ConditionExpression="attribute_not_exists(PK) OR expires_at < :now",
    ExpressionAttributeValues={":now": now},
)
```

`attribute_not_exists(PK)` means "only create this claim if no item with this partition key exists."

`OR expires_at < :now` allows a new worker to recover the claim after the old lease expires.

## Release

```python
table.delete_item(
    Key={"PK": f"CLAIM#{thread_id}", "SK": "RESUME"},
    ConditionExpression="claim_token = :token",
    ExpressionAttributeValues={":token": token},
)
```

Release must prove ownership. Without the token condition, a slow expired worker can delete a newer worker's valid claim.

## Timeline

```text
t=00 worker A acquires token A
t=30 worker A is still running, lease expires
t=31 worker B acquires token B
t=32 worker A finishes and tries release with token A
t=32 DynamoDB rejects delete because stored token is B
```
