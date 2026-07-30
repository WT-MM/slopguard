"""Fixture: original of a fork; diverged_b.py is its drifted copy."""


def sync_inventory(client, warehouse_id, batch_size, logger):
    cursor = None
    synced = 0
    failures = []
    while True:
        page = client.list_items(warehouse_id, cursor=cursor, limit=batch_size)
        for item in page.items:
            record = {
                "sku": item.sku,
                "quantity": item.quantity,
                "location": item.location,
                "updated_at": item.updated_at,
            }
            try:
                client.upsert_record(record)
                synced += 1
            except ConnectionError as exc:
                failures.append((item.sku, exc))
                logger.warning("upsert failed for %s: %s", item.sku, exc)
        if not page.next_cursor:
            break
        cursor = page.next_cursor
    logger.info("synced %d items with %d failures", synced, len(failures))
    return synced, failures
