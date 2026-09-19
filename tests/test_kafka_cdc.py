from src.workers.kafka_cdc import should_trigger_debezium_record


def test_should_trigger_on_insert_pending():
    assert (
        should_trigger_debezium_record(
            {"op": "c", "after": {"id": 10, "status": "pending"}}
        )
        == 10
    )


def test_should_trigger_on_retry_to_pending():
    assert (
        should_trigger_debezium_record(
            {
                "op": "u",
                "before": {"id": 11, "status": "processing"},
                "after": {"id": 11, "status": "pending"},
            }
        )
        == 11
    )


def test_should_not_trigger_done_row():
    assert (
        should_trigger_debezium_record(
            {"op": "u", "after": {"id": 12, "status": "done"}}
        )
        is None
    )
