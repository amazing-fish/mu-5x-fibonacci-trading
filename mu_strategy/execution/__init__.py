"""Non-trading execution planning boundaries."""

__all__ = (
    "ActionSelection",
    "ActorKind",
    "AuditEventType",
    "EXECUTION_AUDIT_SCHEMA_VERSION",
    "EXECUTION_STORE_SCHEMA_VERSION",
    "ORDER_INTENT_FINGERPRINT_FIELDS",
    "ExecutionAuditEvent",
    "ExecutionEnvironment",
    "ExecutionStoreConflictError",
    "ExecutionStoreError",
    "ExecutionStoreInvariantError",
    "ExecutionStoreSchemaError",
    "IntentRevisionAction",
    "IntentRevisionPlan",
    "MutationOperation",
    "MutationReservation",
    "OKXInstrumentSpec",
    "OrderIntent",
    "OrderIntentFactory",
    "OrderIntentIneligibleError",
    "OrderIntentRevisionError",
    "OrderIntentSchemaError",
    "ReservationState",
    "SQLiteExecutionStore",
    "cancel_mutation_action_id",
    "classify_intent_revision",
    "idempotency_key_for",
    "leverage_mutation_action_id",
    "okx_client_order_id_for",
    "render_order_intent_review",
    "submit_mutation_action_id",
)

_AUDIT_EXPORTS = {
    "ActorKind",
    "AuditEventType",
    "EXECUTION_AUDIT_SCHEMA_VERSION",
    "ExecutionAuditEvent",
    "MutationOperation",
    "ReservationState",
    "idempotency_key_for",
    "okx_client_order_id_for",
}

_STORE_EXPORTS = {
    "ActionSelection",
    "EXECUTION_STORE_SCHEMA_VERSION",
    "ExecutionStoreConflictError",
    "ExecutionStoreError",
    "ExecutionStoreInvariantError",
    "ExecutionStoreSchemaError",
    "MutationReservation",
    "SQLiteExecutionStore",
    "cancel_mutation_action_id",
    "leverage_mutation_action_id",
    "submit_mutation_action_id",
}


def __getattr__(name: str):
    if name == "OKXInstrumentSpec":
        from mu_strategy.execution.instruments import OKXInstrumentSpec

        return OKXInstrumentSpec
    if name in _AUDIT_EXPORTS:
        from mu_strategy.execution import audit

        return getattr(audit, name)
    if name in _STORE_EXPORTS:
        from mu_strategy.execution import store

        return getattr(store, name)
    if name in __all__:
        from mu_strategy.execution import intents

        return getattr(intents, name)
    raise AttributeError(name)
