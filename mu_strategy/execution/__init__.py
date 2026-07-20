"""Non-trading execution planning boundaries."""

__all__ = (
    "ORDER_INTENT_FINGERPRINT_FIELDS",
    "ExecutionEnvironment",
    "IntentRevisionAction",
    "IntentRevisionPlan",
    "OKXInstrumentSpec",
    "OrderIntent",
    "OrderIntentFactory",
    "OrderIntentIneligibleError",
    "OrderIntentRevisionError",
    "OrderIntentSchemaError",
    "classify_intent_revision",
    "render_order_intent_review",
)


def __getattr__(name: str):
    if name == "OKXInstrumentSpec":
        from mu_strategy.execution.instruments import OKXInstrumentSpec

        return OKXInstrumentSpec
    if name in __all__:
        from mu_strategy.execution import intents

        return getattr(intents, name)
    raise AttributeError(name)
